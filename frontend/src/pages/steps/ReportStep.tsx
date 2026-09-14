import { AlertCircle, ArrowRight, Download, Info, Lightbulb, RefreshCw } from 'lucide-react';

import EmotionTrajectoryChart from '../../components/EmotionTrajectoryChart';
import KnowledgeCitationPrintAppendix from '../../components/KnowledgeCitationPrintAppendix';
import KnowledgeLinkedText from '../../components/KnowledgeLinkedText';
import { useWorkflow } from '../../context/WorkflowContext';
import { useLanguage } from '../../i18n/LanguageContext';
import type { CoachTaskDraft, CoachTaskResult } from '../../types/domain';
import { normalizeDisplayText } from '../../utils/displayText';
import {
  buildReportImprovementRows,
} from '../../utils/reportIssues';

const DIMENSION_ORDER = [
  { taskId: 'opening_evaluation', fallbackName: '开场定调与结果对齐', englishName: 'Opening alignment and result clarity' },
  { taskId: 'emotion_evaluation', fallbackName: '情绪承接', englishName: 'Emotional acknowledgment' },
  { taskId: 'output_expectations_evaluation', fallbackName: '回归产出与标准', englishName: 'Refocus on outcomes and standards' },
  { taskId: 'development_plan_evaluation', fallbackName: '总结与发展计划', englishName: 'Summary and development plan' },
] as const;

const TASK_TITLES: Readonly<Record<string, readonly [string, string]>> = {
  opening_evaluation: ['开场定调与绩效结果对齐评估', 'Opening alignment and performance-result evaluation'],
  emotion_evaluation: ['情绪承接评估', 'Emotional acknowledgment evaluation'],
  output_expectations_evaluation: ['从情绪回归产出与标准评估', 'Outcomes and standards evaluation'],
  development_plan_evaluation: ['总结与差异化发展计划评估', 'Summary and differentiated development-plan evaluation'],
};

const IMPROVEMENT_TITLES_ENGLISH: Readonly<Record<string, string>> = {
  '沟通基调': 'Communication tone',
  '绩效结果对齐': 'Performance-result alignment',
  '目标方向的正确性': 'Correctness of goal direction',
  '结果的突破性': 'Breakthrough results',
  '高绩效文化': 'High-performance culture',
  '认真倾听，理解情绪': 'Listen carefully and understand emotion',
  '共情总结': 'Empathic summary',
  '共同探索': 'Joint exploration',
  '岗位等级要求': 'Job-level requirements',
  'Career Elements': 'Career Elements',
  '当前需要解决的问题': 'Current issue to address',
  '未标明子维度的改进项': 'Improvement item without a specified subdimension',
};

function renderTaskDraft(
  task: CoachTaskDraft,
  translate: (source: string, english?: string) => string,
) {
  if (task.error) return <p>{normalizeDisplayText(task.error)}</p>;
  if (task.summary) return <p>{normalizeDisplayText(task.summary)}</p>;
  const active = task.status === 'running';
  const text = task.status === 'done'
    ? translate('该部分已完成分析', 'Analysis completed for this section')
    : active
      ? translate('正在整理结构化内容', 'Organizing structured content')
      : translate('等待开始分析', 'Waiting for analysis to begin');
  return (
    <p>
      {text}
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

function dimensionStatus(
  result: CoachTaskResult | undefined,
  translate: (source: string, english?: string) => string,
) {
  if (!result || result.status === 'failed') {
    return { text: translate('评估失败', 'Evaluation failed'), tone: 'danger' };
  }
  if (result.status === 'insufficient_information') {
    return { text: translate('信息不足', 'Insufficient information'), tone: 'warning' };
  }
  return null;
}


export default function ReportStep() {
  const { translate, translateTemplate } = useLanguage();
  const { session, coachReport, coachTasks, reportStatus, ensureReport, exportReport } = useWorkflow();
  const hasManagerTurn = Boolean(session?.conversation?.some((turn) => turn.speaker === 'manager'));
  const progressiveResults = coachTasks
    .map((task) => task.result)
    .filter((result): result is CoachTaskResult => Boolean(result));
  const visibleResults = coachReport?.task_results || progressiveResults;
  const resultsByTask = new Map(
    visibleResults.map((result) => [result.task_id, result]),
  );
  const hasVisibleResults = visibleResults.length > 0;
  const reportPartial = coachReport?.status === 'partial';
  const reportNeedsRetry = reportPartial || reportStatus === 'partial_error';
  const reportCitations = visibleResults.flatMap((result) => result.citations || []);

  return (
    <section id="screen-report" className="screen active">
      <div className="page-intro compact-intro report-page-intro">
        <h1>{translate('复盘报告', 'Review report')}</h1>
        {(coachReport || reportNeedsRetry) && (
          <div className="report-page-actions">
            {reportNeedsRetry && <span className="status-badge warning">{translate('生成未完成', 'Generation incomplete')}</span>}
            {reportNeedsRetry && (
              <button
                type="button"
                className="report-export-button"
                onClick={() => { void ensureReport(true); }}
                aria-label={translate('重新生成复盘报告', 'Regenerate review report')}
                title={translate('重新生成复盘报告', 'Regenerate review report')}
              >
                <RefreshCw size={17} aria-hidden="true" />
                <span>{translate('重新生成', 'Regenerate')}</span>
              </button>
            )}
            {coachReport && (
              <button
                type="button"
                className="report-export-button"
                onClick={exportReport}
                aria-label={translate('导出复盘报告', 'Export review report')}
                title={translate('导出复盘报告', 'Export review report')}
              >
                <Download size={17} aria-hidden="true" />
                <span>{translate('导出报告', 'Export report')}</span>
              </button>
            )}
          </div>
        )}
      </div>
      <section className="report-view">
        {!coachReport && !hasManagerTurn && (
          <section className="soft-card empty-report">
            <h2>{translate('请先完成至少一轮多轮预演', 'Complete at least one rehearsal turn first')}</h2>
            <p className="muted">{translate(
              '复盘报告需要管理者原话和员工回复作为评估依据。',
              'The review report uses the manager’s exact wording and the employee’s responses as evaluation evidence.',
            )}</p>
          </section>
        )}
        {!coachReport && hasManagerTurn && (
          <div className="briefing-layout report-generation-layout">
            <section className="soft-card guidance-card briefing-card report-generation-card">
              <div className="guidance-list report-generation-list">
                {coachTasks.map((task) => {
                  const taskTitle = TASK_TITLES[task.task_id];
                  return (
                    <section className="guidance-section report-generation-section" key={task.task_id}>
                      <div>
                        <div className="stream-section-title">
                          <h3>{taskTitle
                            ? translate(taskTitle[0], taskTitle[1])
                            : normalizeDisplayText(task.task_name)}</h3>
                          {(task.status === 'idle' || task.status === 'error') && (
                            <span className={`status-badge ${task.status === 'error' ? 'danger' : 'neutral'}`}>
                              {task.status === 'error'
                                ? translate('失败', 'Failed')
                                : translate('等待中', 'Waiting')}
                            </span>
                          )}
                        </div>
                        {renderTaskDraft(task, translate)}
                        {task.status === 'running' && !task.summary && !task.error && <div className="skeleton-line" />}
                      </div>
                    </section>
                  );
                })}
              </div>
            </section>
          </div>
        )}
        {hasVisibleResults && (
          <div className="dimension-report-list">
            {DIMENSION_ORDER.map((dimension, index) => {
              const result = resultsByTask.get(dimension.taskId);
              if (!result) return null;

              const scoreDetail = result.dimension_scores?.[0];
              const name = translate(dimension.fallbackName, dimension.englishName);
              const level = scoreDetail?.level ? normalizeDisplayText(scoreDetail.level) : undefined;
              const basisText = scoreDetail?.basis || scoreDetail?.comment;
              const basis = basisText ? normalizeDisplayText(basisText) : undefined;
              const basisTarget = scoreDetail?.basis
                ? 'dimension_scores.0.basis'
                : 'dimension_scores.0.comment';
              const status = dimensionStatus(result, translate);
              const strengths = (result.strengths || [])
                .map((item) => normalizeDisplayText(item.trim()))
                .filter(Boolean);
              const improvementRows = buildReportImprovementRows(result);
              const careerAdvice = result.career_elements_advice || [];
              const displayScore = typeof result.score === 'number'
                ? result.score
                : scoreDetail?.score;
              const hasScore = typeof displayScore === 'number';
              const improvementEmptyCopy = (() => {
                if (result.status === 'failed') {
                  return {
                    finding: translate('该维度评估失败，未生成改进内容。', 'This dimension failed to evaluate, so no improvement content was generated.'),
                    suggestion: translate('请重新生成报告后查看具体建议。', 'Regenerate the report to view specific suggestions.'),
                  };
                }
                if (result.status === 'insufficient_information') {
                  return {
                    finding: translate('当前对话信息不足，尚无法形成改进判断。', 'The current conversation does not provide enough information to identify improvements.'),
                    suggestion: translate('补充完成相关对话后再生成具体建议。', 'Complete the relevant conversation and regenerate specific suggestions.'),
                  };
                }
                if (typeof displayScore === 'number' && displayScore <= 3) {
                  return {
                    finding: translateTemplate(
                      '该维度评分为 {score} 分，但评估结果未返回与评分对应的改进问题。',
                      'This dimension scored {score}, but the evaluation did not return a corresponding improvement issue.',
                      { score: displayScore },
                    ),
                    suggestion: translate('请重新生成该维度评估，以获取可核验的具体建议。', 'Regenerate this dimension to receive a specific, verifiable suggestion.'),
                  };
                }
                if (displayScore === 5) {
                  return {
                    finding: translate('本轮评分为 5 分，未返回明确改进问题。', 'This turn scored 5 and returned no explicit improvement issue.'),
                    suggestion: translate('本轮未返回对应的具体建议。', 'No corresponding suggestion was returned for this turn.'),
                  };
                }
                if (displayScore === 4) {
                  return {
                    finding: translate('本轮评分为 4 分，未返回有经理原话支持的明确改进问题。', 'This turn scored 4 and returned no explicit improvement issue supported by the manager’s exact wording.'),
                    suggestion: translate('本轮未返回对应的具体建议。', 'No corresponding suggestion was returned for this turn.'),
                  };
                }
                return {
                  finding: translate('该维度评估结果未返回分数或明确改进问题。', 'This dimension returned neither a score nor an explicit improvement issue.'),
                  suggestion: translate('请重新生成该维度评估后查看具体建议。', 'Regenerate this dimension to view specific suggestions.'),
                };
              })();
              const basisTooltipId = basis
                ? `dimension-score-basis-${dimension.taskId}`
                : undefined;

              return (
                <article className={`dimension-report ${result.status === 'failed' ? 'is-failed' : ''}`} key={dimension.taskId}>
                  <header className="dimension-report-header">
                    <span className="dimension-index">{String(index + 1).padStart(2, '0')}</span>
                    <div className="dimension-heading">
                      <div className="dimension-heading-top">
                        <div className="dimension-title-row">
                          <h3>{name}</h3>
                          {status && <span className={`status-badge ${status.tone}`}>{status.text}</span>}
                        </div>
                        <div
                          className="dimension-score"
                          role="group"
                          aria-label={hasScore
                            ? level
                              ? translateTemplate(
                                '{name} {score} 分，满分 5 分，{level}',
                                '{name}: {score} out of 5, {level}',
                                { name, score: displayScore, level },
                              )
                              : translateTemplate(
                                '{name} {score} 分，满分 5 分',
                                '{name}: {score} out of 5',
                                { name, score: displayScore },
                              )
                            : level
                              ? translateTemplate(
                                '{name}暂无分数，{level}',
                                '{name}: no score, {level}',
                                { name, level },
                              )
                              : translateTemplate(
                                '{name}暂无分数',
                                '{name}: no score',
                                { name },
                              )}
                        >
                          <div className="dimension-score-line">
                            <div className="dimension-score-value" aria-hidden="true">
                              <strong>{hasScore ? displayScore : '--'}</strong>
                              <span className={hasScore ? undefined : 'is-placeholder'}>/5</span>
                            </div>
                            {level && <small>{level}</small>}
                            {basis && basisTooltipId && (
                              <span className="dimension-score-basis">
                                <button
                                  type="button"
                                  className="dimension-score-basis-trigger"
                                  aria-label={translateTemplate(
                                    '查看{name}评分依据',
                                    'View the scoring basis for {name}',
                                    { name },
                                  )}
                                  aria-describedby={basisTooltipId}
                                >
                                  <Info size={15} aria-hidden="true" />
                                </button>
                                <span
                                  id={basisTooltipId}
                                  className="dimension-score-basis-tooltip"
                                  role="tooltip"
                                >
                                  <strong>{translate('评分依据', 'Scoring basis')}</strong>
                                  <span>{basis}</span>
                                </span>
                              </span>
                            )}
                          </div>
                        </div>
                      </div>
                      <p className="dimension-summary">
                        <KnowledgeLinkedText
                          text={result.summary || translate('该维度未返回评估内容。', 'No evaluation content was returned for this dimension.')}
                          target="summary"
                          citations={result.citations}
                          showRelatedSources={false}
                        />
                      </p>
                      {strengths.length > 0 && (
                        <p className="dimension-strengths">
                          <strong className="dimension-strength-label">{translate('做得好的地方：', 'What went well: ')}</strong>
                          {strengths.join('')}
                        </p>
                      )}
                      {basis && (
                        <p className="dimension-basis dimension-basis-print">
                          <strong>{translate('评分依据', 'Scoring basis')}</strong>
                          <KnowledgeLinkedText
                            text={basis}
                            target={basisTarget}
                            citations={result.citations}
                            showRelatedSources={false}
                          />
                        </p>
                      )}
                    </div>
                  </header>

                  {dimension.taskId === 'emotion_evaluation' && (
                    <EmotionTrajectoryChart turns={session?.conversation} />
                  )}

                  <div className="dimension-report-body">
                    <div className="improvement-comparison-header">
                      <div className="dimension-findings improvement-column-heading">
                        <div className="dimension-section-title">
                          <AlertCircle size={19} aria-hidden="true" />
                          <h4>{translate('需要改进', 'Needs improvement')}</h4>
                        </div>
                      </div>
                      <div className="dimension-actions improvement-column-heading">
                        <div className="dimension-section-title">
                          <Lightbulb size={19} aria-hidden="true" />
                          <h4>{translate('具体建议', 'Specific suggestion')}</h4>
                        </div>
                      </div>
                    </div>

                    {improvementRows.length ? (
                      <ol className="improvement-comparison-list">
                        {improvementRows.map(({ issue, phrase, phraseSourceIndex, sourceIndex, title, titleIndex }, improvementIndex) => {
                          const rawRowTitle = title || issue?.title || null;
                          const localizedBaseTitle = rawRowTitle
                            ? translate(rawRowTitle, IMPROVEMENT_TITLES_ENGLISH[rawRowTitle])
                            : null;
                          const rowTitle = titleIndex && localizedBaseTitle
                            ? translateTemplate(
                              '{title} {index}',
                              '{title} {index}',
                              { title: localizedBaseTitle, index: titleIndex },
                            )
                            : localizedBaseTitle || (improvementRows.length > 1
                              ? translateTemplate(
                                '改进项 {index}',
                                'Improvement {index}',
                                { index: improvementIndex + 1 },
                              )
                              : null);
                          const previousRow = improvementIndex > 0
                            ? improvementRows[improvementIndex - 1]
                            : undefined;
                          const previousRawRowTitle = previousRow
                            ? previousRow.title || previousRow.issue?.title || null
                            : null;
                          const usesFallbackTitle = !localizedBaseTitle && improvementRows.length > 1;
                          const showRowTitle = Boolean(
                            rowTitle
                            && (usesFallbackTitle || titleIndex || rawRowTitle !== previousRawRowTitle),
                          );
                          const isPlaceholderOnly = Boolean(issue?.isPlaceholder && !phrase);
                          return (
                            <li
                              className="improvement-comparison-row"
                              key={`${sourceIndex}-${issue?.sourceIndex ?? 'none'}-${phraseSourceIndex ?? 'none'}-${issue?.text || phrase?.suggestion || 'improvement'}`}
                            >
                              <section className="dimension-findings" aria-label={translateTemplate(
                                '{title}：需要改进',
                                '{title}: needs improvement',
                                { title: rowTitle || translate('本项', 'This item') },
                              )}>
                                <span className="improvement-cell-label">{translate('需要改进', 'Needs improvement')}</span>
                                {showRowTitle && (
                                  <strong className="improvement-row-title">{rowTitle}</strong>
                                )}
                                <div className="issue-dimension-copy">
                                  {issue && !issue.isPlaceholder ? (
                                    <KnowledgeLinkedText
                                      text={issue.text}
                                      target={`improvement_points.${issue.sourceIndex}`}
                                      citations={result.citations}
                                      showRelatedSources={false}
                                    />
                                  ) : issue ? (
                                    <span className="dimension-empty">{translate(
                                      issue.text,
                                      issue.text === '历史报告未记录该子维度的判断。'
                                        ? 'The historical report did not record a judgment for this subdimension.'
                                        : undefined,
                                    )}</span>
                                  ) : (
                                    <span className="dimension-empty">{translate('本项未返回明确的改进重点。', 'No explicit improvement focus was returned for this item.')}</span>
                                  )}
                                </div>
                              </section>
                              <section className="dimension-actions" aria-label={translateTemplate(
                                '{title}：具体建议',
                                '{title}: specific suggestion',
                                { title: rowTitle || translate('本项', 'This item') },
                              )}>
                                <span className="improvement-cell-label">{translate('具体建议', 'Specific suggestion')}</span>
                                {isPlaceholderOnly ? (
                                  <p className="dimension-empty">{translate('历史报告未记录该子维度的具体建议。', 'The historical report did not record a specific suggestion for this subdimension.')}</p>
                                ) : (
                                  <>
                                    {phrase?.original?.trim() ? (
                                      <p className="original-phrase"><span>{translate('经理实际表达', 'Manager’s exact wording')}</span>{phrase.original}</p>
                                    ) : (
                                      <p className="missing-manager-expression">{translate('本项未记录可核验的经理原话。', 'No verifiable manager wording was recorded for this item.')}</p>
                                    )}
                                    {phrase?.suggestion?.trim() ? (
                                      <>
                                        <div className="suggested-phrase">
                                          <ArrowRight size={17} aria-hidden="true" />
                                          <strong>
                                            <KnowledgeLinkedText
                                              text={phrase.suggestion}
                                              target={`better_phrases.${phraseSourceIndex ?? sourceIndex}.suggestion`}
                                              citations={result.citations}
                                              showRelatedSources={false}
                                            />
                                          </strong>
                                        </div>
                                        {phrase.reason?.trim() && (
                                          <p className="improvement-reason">
                                            <KnowledgeLinkedText
                                              text={phrase.reason}
                                              target={`better_phrases.${phraseSourceIndex ?? sourceIndex}.reason`}
                                              citations={result.citations}
                                              showRelatedSources={false}
                                            />
                                          </p>
                                        )}
                                      </>
                                    ) : (
                                      <p className="dimension-empty">{translate('本项未返回可执行的具体建议。', 'No actionable specific suggestion was returned for this item.')}</p>
                                    )}
                                  </>
                                )}
                              </section>
                            </li>
                          );
                        })}
                      </ol>
                    ) : (
                      <div className="improvement-comparison-empty">
                        <section className="dimension-findings">
                          <span className="improvement-cell-label">{translate('需要改进', 'Needs improvement')}</span>
                          <p className="dimension-empty">{improvementEmptyCopy.finding}</p>
                        </section>
                        <section className="dimension-actions">
                          <span className="improvement-cell-label">{translate('具体建议', 'Specific suggestion')}</span>
                          <p className="dimension-empty">{improvementEmptyCopy.suggestion}</p>
                        </section>
                      </div>
                    )}
                  </div>
                  {careerAdvice.length > 0 && (
                    <section className="career-elements-advice">
                      <div className="dimension-section-title">
                        <Lightbulb size={19} aria-hidden="true" />
                        <h4>{translate('Career Elements 发展建议', 'Career Elements development suggestions')}</h4>
                      </div>
                      <ol>
                        {careerAdvice.map((advice, adviceIndex) => {
                          const targetPrefix = `career_elements_advice.${adviceIndex}`;
                          return (
                            <li key={`${adviceIndex}-${advice.element}`}>
                              <h5>
                                <KnowledgeLinkedText
                                  text={advice.element}
                                  target={`${targetPrefix}.element`}
                                  citations={result.citations}
                                  showRelatedSources={false}
                                />
                              </h5>
                              <p>
                                <strong>{translate('行动建议：', 'Suggested action: ')}</strong>
                                <KnowledgeLinkedText
                                  text={advice.suggestion}
                                  target={`${targetPrefix}.suggestion`}
                                  citations={result.citations}
                                  showRelatedSources={false}
                                />
                              </p>
                              <p className="career-elements-reason">
                                <strong>{translate('建议依据：', 'Rationale: ')}</strong>
                                <KnowledgeLinkedText
                                  text={advice.reason}
                                  target={`${targetPrefix}.reason`}
                                  citations={result.citations}
                                  showRelatedSources={false}
                                />
                              </p>
                            </li>
                          );
                        })}
                      </ol>
                    </section>
                  )}
                </article>
              );
            })}
          </div>
        )}

        <KnowledgeCitationPrintAppendix citations={reportCitations} />

        {coachReport?.disclaimer && <p className="report-disclaimer">{normalizeDisplayText(coachReport.disclaimer)}</p>}
      </section>
    </section>
  );
}

import {
  AlertTriangle,
  ArrowRight,
  CheckCircle2,
  ClipboardList,
  ExternalLink,
  FileText,
  LoaderCircle,
  MessageSquareText,
  RefreshCw,
  UserRound,
  X,
} from 'lucide-react';
import {
  type KeyboardEvent as ReactKeyboardEvent,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { useDialogFocus } from '../../hooks/useDialogFocus';
import { intentName, motiveName } from '../../i18n/businessLabels';
import { languageName, useLanguage, type AppLanguage } from '../../i18n/LanguageContext';
import type {
  AdminExportConversation,
  AdminUsageBetterPhrase,
  AdminUsageCareerElementAdvice,
  AdminUsageCoachTask,
  AdminUsageGuidance,
  AdminUsageRisk,
  AdminUsageSessionDetail,
} from '../../types/auth';
import { profileRows, profileValueToText } from '../../utils/format';
import {
  adminUsageSessionPath,
  type AdminUsageSection,
} from '../../utils/adminUsageSession';

interface AdminUsageSessionDialogProps {
  conversation: AdminExportConversation;
  detail: AdminUsageSessionDetail | null;
  loading: boolean;
  error: string;
  onClose: () => void;
  onRetry: () => void;
  onOpenPage?: () => void;
}

interface AdminUsageSessionViewerProps {
  detail: AdminUsageSessionDetail;
  activeSection: AdminUsageSection;
  onSectionChange: (section: AdminUsageSection) => void;
}

type Translate = (source: string, english?: string) => string;

function translatedTemplate(
  translate: Translate,
  source: string,
  english: string,
  values: Readonly<Record<string, string | number>>,
): string {
  return translate(source, english).replace(/\{([a-z_]+)\}/giu, (placeholder, key: string) => (
    Object.prototype.hasOwnProperty.call(values, key) ? String(values[key]) : placeholder
  ));
}

const BEIJING_DATE_TIME: Record<AppLanguage, Intl.DateTimeFormat> = {
  'zh-CN': new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }),
  en: new Intl.DateTimeFormat('en-US', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }),
  de: new Intl.DateTimeFormat('de-DE', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }),
  ja: new Intl.DateTimeFormat('ja-JP', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }),
};

const PROFILE_LABELS_ENGLISH: Readonly<Record<string, string>> = {
  '工号': 'Employee ID',
  '姓名': 'Name',
  '员工代称': 'Employee alias',
  '岗位': 'Role',
  '部门': 'Department',
  '职级': 'Level',
  '汇报关系': 'Reporting line',
  '当前绩效评级': 'Current performance rating',
  '考核周期': 'Review cycle',
  '本次谈话主题': 'Conversation topic',
  '关键目标': 'Key goals',
  '完成情况': 'Completion status',
  '历史反馈': 'Previous feedback',
  '管理动作': 'Management actions',
  '员工状态': 'Employee status',
  '敏感约束': 'Sensitive constraints',
  '额外提供的信息': 'Additional information',
};

const PERSONALITY_FIELDS = [
  ['openness', '开放性', 'Openness'],
  ['conscientiousness', '尽责性', 'Conscientiousness'],
  ['extraversion', '外向性', 'Extraversion'],
  ['agreeableness', '宜人性', 'Agreeableness'],
  ['neuroticism', '情绪敏感性', 'Emotional sensitivity'],
] as const;

const MOTIVE_NAMES_ZH: Readonly<Record<string, string>> = {
  career: '职业发展',
  commerce: '薪酬回报',
  power: '影响力',
  recognition: '认可',
  affiliation: '团队归属',
  security: '安全感',
  hedonism: '舒适的工作环境',
};

const GUIDANCE_DIMENSIONS: Readonly<Record<string, readonly [string, string]>> = {
  start: ['开场定调与结果对齐', 'Opening and result alignment'],
  emotion: ['情绪承接', 'Emotional acknowledgment'],
  requirement: ['产出与标准', 'Outcomes and standards'],
  plan: ['总结与发展计划', 'Summary and development plan'],
};

const DIAGNOSTIC_DIMENSION_LABELS: Readonly<Record<string, readonly [string, string]>> = {
  communication_tone: ['沟通基调', 'Communication tone'],
  performance_result_alignment: ['绩效结果对齐', 'Performance result alignment'],
  goal_direction_correctness: ['目标方向的正确性', 'Goal direction correctness'],
  result_breakthrough: ['结果的突破性', 'Breakthrough in results'],
  high_performance_culture: ['高绩效文化', 'High-performance culture'],
  listening_and_emotion_understanding: ['认真倾听，理解情绪', 'Listening and emotional understanding'],
  empathic_summary: ['共情总结', 'Empathic summary'],
  joint_exploration: ['共同探索', 'Joint exploration'],
  job_level_requirements: ['岗位等级要求', 'Job-level requirements'],
  career_elements: ['Career Elements', 'Career Elements'],
};

const TASK_DIAGNOSTIC_DIMENSIONS: Readonly<Record<string, readonly string[]>> = {
  opening_evaluation: ['communication_tone', 'performance_result_alignment'],
  start: ['communication_tone', 'performance_result_alignment'],
  output_expectations_evaluation: [
    'goal_direction_correctness',
    'result_breakthrough',
    'high_performance_culture',
  ],
  emotion_evaluation: [
    'listening_and_emotion_understanding',
    'empathic_summary',
    'joint_exploration',
  ],
  emotion: [
    'listening_and_emotion_understanding',
    'empathic_summary',
    'joint_exploration',
  ],
  development_plan_evaluation: ['job_level_requirements', 'career_elements'],
  plan: ['job_level_requirements', 'career_elements'],
  requirement: [
    'goal_direction_correctness',
    'result_breakthrough',
    'high_performance_culture',
  ],
};

const COACH_TASK_TITLES: Readonly<Record<string, readonly [string, string]>> = {
  opening_evaluation: ['开场定调与绩效结果对齐评估', 'Opening alignment and performance-result evaluation'],
  start: ['开场定调与绩效结果对齐评估', 'Opening alignment and performance-result evaluation'],
  emotion_evaluation: ['情绪承接评估', 'Emotional acknowledgment evaluation'],
  emotion: ['情绪承接评估', 'Emotional acknowledgment evaluation'],
  output_expectations_evaluation: ['从情绪回归产出与标准评估', 'Outcomes and standards evaluation'],
  requirement: ['从情绪回归产出与标准评估', 'Outcomes and standards evaluation'],
  development_plan_evaluation: ['总结与差异化发展计划评估', 'Summary and differentiated development-plan evaluation'],
  plan: ['总结与差异化发展计划评估', 'Summary and differentiated development-plan evaluation'],
};

function displayDate(
  value: string | null | undefined,
  language: AppLanguage,
  translate: Translate,
): string {
  if (!value) return translate('未记录', 'Not recorded');
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return translate('未记录', 'Not recorded');
  return BEIJING_DATE_TIME[language].format(parsed);
}

function displayEmployeeName(detail: AdminUsageSessionDetail | null, fallback: string | null | undefined) {
  const profile = detail?.employee_profile || {};
  return profileValueToText(profile.name)
    || profileValueToText(profile.employee_alias)
    || profileValueToText(profile.employee_id)
    || fallback
    || '';
}

function stageLabel(value: string | null | undefined, translate: Translate): string {
  const labels: Record<string, readonly [string, string]> = {
    created: ['已创建', 'Created'],
    profile_ready: ['员工资料已确认', 'Profile confirmed'],
    setup_ready: ['设置已完成', 'Setup complete'],
    guidance_ready: ['谈前指导已生成', 'Guidance generated'],
    rehearsal: ['多轮预演', 'Rehearsal'],
    report_ready: ['复盘报告已生成', 'Review generated'],
    ended: ['会话已结束', 'Session ended'],
  };
  const match = labels[String(value || '')];
  return match ? translate(match[0], match[1]) : String(value || translate('未记录', 'Not recorded'));
}

function runModeLabel(value: string | null | undefined, translate: Translate): string {
  const labels: Record<string, readonly [string, string]> = {
    guidance_only: ['仅谈前指导', 'Guidance only'],
    guidance_then_rehearsal: ['谈前指导后进入预演', 'Guidance followed by rehearsal'],
    rehearsal_report: ['预演与复盘', 'Rehearsal and review'],
  };
  const match = labels[String(value || '')];
  return match ? translate(match[0], match[1]) : String(value || translate('未记录', 'Not recorded'));
}

function taskStatusLabel(value: string, translate: Translate): string {
  const labels: Record<string, readonly [string, string]> = {
    success: ['已完成', 'Completed'],
    insufficient_information: ['信息不足', 'Insufficient information'],
    failed: ['生成失败', 'Failed'],
  };
  const match = labels[value];
  return match ? translate(match[0], match[1]) : value;
}

function speakerLabel(speaker: string, translate: Translate): string {
  const normalized = speaker.toLowerCase();
  if (normalized === 'manager' || normalized === 'user' || normalized === 'you') {
    return translate('经理', 'Manager');
  }
  if (normalized === 'employee' || normalized === 'assistant') {
    return translate('员工', 'Employee');
  }
  return translate('系统', 'System');
}

function EmptyDetail({ text }: { text: string }) {
  return (
    <div className="admin-usage-detail-empty">
      <FileText size={22} aria-hidden="true" />
      <p>{text}</p>
    </div>
  );
}

function StringList({ values }: { values: readonly string[] }) {
  return (
    <ul className="admin-usage-detail-list">
      {values.map((value, index) => <li key={`${index}-${value}`}>{value}</li>)}
    </ul>
  );
}

function OverviewPanel({ detail }: { detail: AdminUsageSessionDetail }) {
  const { language, translate } = useLanguage();
  const employeeName = displayEmployeeName(detail, null);
  const rows = detail.employee_profile ? profileRows(detail.employee_profile) : [];
  const intentId = detail.intent?.intent_id;
  const localizedIntent = intentName(intentId, detail.intent?.name || intentId, language);
  const primaryMotiveId = detail.motivation?.primary_motive_id;
  const secondaryMotiveIds = detail.motivation?.secondary_motive_ids || [];
  const motiveLabel = (id: string | null | undefined) => motiveName(
    id,
    MOTIVE_NAMES_ZH[String(id || '')] || id,
    language,
  );

  return (
    <div className="admin-usage-detail-stack">
      <section className="admin-usage-detail-section">
        <h3>{translate('会话概览', 'Session overview')}</h3>
        <dl className="admin-usage-overview-grid">
          <div><dt>{translate('用户', 'User')}</dt><dd>{detail.user_display_name || detail.user_email.split('@')[0]}</dd></div>
          <div><dt>{translate('用户邮箱', 'User email')}</dt><dd>{detail.user_email}</dd></div>
          <div><dt>{translate('员工', 'Employee')}</dt><dd>{employeeName || translate('未记录', 'Not recorded')}</dd></div>
          <div><dt>{translate('会话状态', 'Session status')}</dt><dd>{stageLabel(detail.stage, translate)}</dd></div>
          <div><dt>{translate('使用流程', 'Workflow')}</dt><dd>{runModeLabel(detail.run_mode, translate)}</dd></div>
          <div><dt>{translate('会话语言', 'Session language')}</dt><dd>{detail.locale ? languageName(detail.locale) : translate('未记录', 'Not recorded')}</dd></div>
          <div><dt>{translate('创建时间', 'Created')}</dt><dd>{displayDate(detail.session_created_at, language, translate)}</dd></div>
          <div><dt>{translate('最后更新', 'Last updated')}</dt><dd>{displayDate(detail.session_updated_at, language, translate)}</dd></div>
          <div><dt>{translate('预演结束', 'Rehearsal ended')}</dt><dd>{displayDate(detail.rehearsal_ended_at, language, translate)}</dd></div>
          <div><dt>{translate('会话编号', 'Session ID')}</dt><dd className="admin-usage-monospace">{detail.session_id}</dd></div>
        </dl>
      </section>

      <section className="admin-usage-detail-section">
        <h3>{translate('员工资料', 'Employee profile')}</h3>
        {rows.length ? (
          <dl className="admin-usage-profile-grid">
            {rows.map(([key, label, value]) => (
              <div key={`${key}-${label}`}>
                <dt>{translate(label, PROFILE_LABELS_ENGLISH[label])}</dt>
                <dd>{value}</dd>
              </div>
            ))}
          </dl>
        ) : <p className="admin-usage-detail-muted">{translate('本次会话未保存员工资料。', 'No employee profile was saved for this session.')}</p>}
        {detail.supplemental_info && (
          <div className="admin-usage-detail-note">
            <strong>{translate('补充信息', 'Additional information')}</strong>
            <p>{detail.supplemental_info}</p>
          </div>
        )}
      </section>

      <section className="admin-usage-detail-section">
        <h3>{translate('沟通意图与当前表现', 'Conversation intent and current performance')}</h3>
        {detail.intent ? (
          <div className="admin-usage-detail-stack is-compact">
            <div className="admin-usage-detail-callout">
              <strong>{localizedIntent || translate('未记录沟通意图', 'Conversation intent not recorded')}</strong>
              {detail.intent.performance_context && <p>{detail.intent.performance_context}</p>}
            </div>
            {(detail.intent.performance_items || []).map((item, index) => (
              <article className="admin-usage-performance-item" key={`${index}-${item.goal}`}>
                <h4>{item.goal}</h4>
                <p>{item.current_performance}</p>
              </article>
            ))}
          </div>
        ) : <p className="admin-usage-detail-muted">{translate('本次会话未保存沟通意图。', 'No conversation intent was saved for this session.')}</p>}
      </section>

      <section className="admin-usage-detail-section">
        <h3>{translate('人格与诉求', 'Personality and motives')}</h3>
        {detail.personality ? (
          <div className="admin-usage-personality-grid">
            {PERSONALITY_FIELDS.map(([key, zh, en]) => {
              const score = detail.personality?.[key];
              return (
                <div key={key}>
                  <span>{translate(zh, en)}</span>
                  <strong>{typeof score === 'number' ? score : '—'}</strong>
                  <div aria-hidden="true"><i style={{ width: `${Math.max(0, Math.min(100, Number(score) || 0))}%` }} /></div>
                </div>
              );
            })}
          </div>
        ) : <p className="admin-usage-detail-muted">{translate('本次会话未保存人格设置。', 'No personality settings were saved for this session.')}</p>}
        <dl className="admin-usage-motive-grid">
          <div>
            <dt>{translate('主要诉求', 'Primary motive')}</dt>
            <dd>{primaryMotiveId ? motiveLabel(primaryMotiveId) : translate('未记录', 'Not recorded')}</dd>
          </div>
          <div>
            <dt>{translate('辅助诉求', 'Secondary motives')}</dt>
            <dd>{secondaryMotiveIds.length
              ? secondaryMotiveIds.map(motiveLabel).join(translate('、', ', '))
              : translate('未记录', 'Not recorded')}</dd>
          </div>
        </dl>
      </section>

      <section className="admin-usage-detail-section">
        <h3>{translate('预演运行备注', 'Rehearsal notes')}</h3>
        {(detail.runtime_notes || []).length
          ? <StringList values={detail.runtime_notes || []} />
          : <p className="admin-usage-detail-muted">{translate('本次会话没有运行备注。', 'This session has no rehearsal notes.')}</p>}
      </section>
    </div>
  );
}

function GuidancePanel({ guidance }: { guidance: AdminUsageGuidance | null | undefined }) {
  const { translate } = useLanguage();
  if (!guidance) {
    return <EmptyDetail text={translate('本次会话未生成谈前指导。', 'No pre-conversation guidance was generated for this session.')} />;
  }
  const dimensionEntries = Object.entries(guidance.dimension_points || {})
    .filter(([, groups]) => Array.isArray(groups) && groups.length > 0);
  const legacySections = [
    [translate('重点风险', 'Key risks'), guidance.risk_preview || []],
    [translate('回应策略', 'Response strategies'), guidance.response_strategies || []],
    [translate('建议表达', 'Suggested wording'), guidance.safer_phrases || []],
  ] as const;
  const hasContent = Boolean(
    guidance.purpose
    || guidance.opening_suggestion
    || dimensionEntries.length
    || legacySections.some(([, values]) => values.length),
  );
  if (!hasContent) {
    return <EmptyDetail text={translate('谈前指导没有可展示的内容。', 'The guidance has no displayable content.')} />;
  }

  return (
    <div className="admin-usage-detail-stack">
      {(guidance.purpose || guidance.opening_suggestion) && (
        <section className="admin-usage-detail-section">
          <h3>{translate('指导概要', 'Guidance overview')}</h3>
          {guidance.purpose && (
            <div className="admin-usage-detail-callout">
              <strong>{translate('沟通目标', 'Conversation objective')}</strong>
              <p>{guidance.purpose}</p>
            </div>
          )}
          {guidance.opening_suggestion && (
            <div className="admin-usage-detail-callout">
              <strong>{translate('开场建议', 'Opening suggestion')}</strong>
              <p>{guidance.opening_suggestion}</p>
            </div>
          )}
        </section>
      )}

      {dimensionEntries.map(([dimensionId, groups]) => {
        const label = GUIDANCE_DIMENSIONS[dimensionId];
        return (
          <section className="admin-usage-detail-section" key={dimensionId}>
            <h3>{label ? translate(label[0], label[1]) : dimensionId}</h3>
            <div className="admin-usage-guidance-groups">
              {groups.map((group, index) => (
                <article key={`${dimensionId}-${index}-${group.title || ''}`}>
                  {group.title && <h4>{group.title}</h4>}
                  {group.summary && <p>{group.summary}</p>}
                  {(group.details || []).length > 0 && <StringList values={group.details || []} />}
                </article>
              ))}
            </div>
          </section>
        );
      })}

      {legacySections.map(([title, values]) => values.length > 0 && (
        <section className="admin-usage-detail-section" key={title}>
          <h3>{title}</h3>
          <StringList values={values} />
        </section>
      ))}
    </div>
  );
}

function ConversationPanel({ detail }: { detail: AdminUsageSessionDetail }) {
  const { language, translate } = useLanguage();
  const turns = (detail.conversation || []).filter((turn) => turn.text.trim());
  const employeeName = displayEmployeeName(detail, translate('员工', 'Employee'));
  if (!turns.length) {
    return <EmptyDetail text={translate('本次会话没有保存有效的预演对话。', 'No rehearsal conversation was saved for this session.')} />;
  }

  return (
    <div className="admin-usage-transcript" role="list">
      {turns.map((turn, index) => {
        const normalizedSpeaker = turn.speaker.toLowerCase();
        const manager = ['manager', 'user', 'you'].includes(normalizedSpeaker);
        const speaker = manager ? translate('经理', 'Manager') : employeeName;
        return (
          <article
            className={`admin-usage-transcript-turn ${manager ? 'is-manager' : 'is-employee'}`}
            key={`${turn.turn_index ?? index}-${turn.speaker}-${index}`}
            role="listitem"
          >
            <header>
              <strong>{speaker}</strong>
              <span>
                {turn.turn_index != null
                  ? translatedTemplate(
                    translate,
                    '第 {turn} 轮',
                    'Turn {turn}',
                    { turn: turn.turn_index },
                  )
                  : speakerLabel(turn.speaker, translate)}
                {turn.created_at ? ` · ${displayDate(turn.created_at, language, translate)}` : ''}
              </span>
            </header>
            <p>{turn.text}</p>
          </article>
        );
      })}
    </div>
  );
}

function renderRisk(risk: AdminUsageRisk | string): string {
  if (typeof risk === 'string') return risk;
  return [risk.category, risk.explanation, risk.safer_phrase]
    .filter(Boolean)
    .join('：');
}

interface AdminImprovementRow {
  sourceIndex: number;
  title: string;
  issue?: string;
  phrase?: AdminUsageBetterPhrase;
}

function normalizedBetterPhrase(
  value: AdminUsageBetterPhrase | string | undefined,
): AdminUsageBetterPhrase | undefined {
  if (typeof value === 'string') {
    const suggestion = value.trim();
    return suggestion ? { suggestion } : undefined;
  }
  if (!value) return undefined;
  const original = value.original?.trim() || undefined;
  const suggestion = value.suggestion?.trim() || undefined;
  const reason = value.reason?.trim() || undefined;
  if (!original && !suggestion && !reason) return undefined;
  return {
    diagnostic_dimension_id: value.diagnostic_dimension_id?.trim() || undefined,
    original,
    suggestion,
    reason,
  };
}

function buildAdminImprovementRows(
  task: AdminUsageCoachTask,
  translate: Translate,
): AdminImprovementRow[] {
  const issues = task.improvement_points || [];
  const phrases = task.better_phrases || [];
  const rowCount = Math.max(issues.length, phrases.length);
  const fixedDimensions = TASK_DIAGNOSTIC_DIMENSIONS[task.task_id] || [];
  const hasDeclaredDimension = phrases.some((value) => (
    typeof value !== 'string' && Boolean(value.diagnostic_dimension_id?.trim())
  ));
  const useLegacyPositions = !hasDeclaredDimension
    && rowCount === fixedDimensions.length;

  return Array.from({ length: rowCount }, (_, sourceIndex) => {
    const issue = issues[sourceIndex]?.trim() || undefined;
    const rawPhrase = phrases[sourceIndex];
    const phrase = normalizedBetterPhrase(rawPhrase);
    const declaredDimensionId = typeof rawPhrase === 'string'
      ? undefined
      : rawPhrase?.diagnostic_dimension_id?.trim() || undefined;
    const dimensionId = declaredDimensionId
      || (useLegacyPositions ? fixedDimensions[sourceIndex] : undefined);
    const dimensionLabel = dimensionId
      ? DIAGNOSTIC_DIMENSION_LABELS[dimensionId]
      : undefined;
    const title = dimensionLabel
      ? translate(dimensionLabel[0], dimensionLabel[1])
      : task.task_id === 'emotion_evaluation' && rowCount === 1
        ? translate('当前需要解决的问题', 'Current issue to address')
        : translatedTemplate(
          translate,
          '改进项 {index}',
          'Improvement {index}',
          { index: sourceIndex + 1 },
        );
    return { sourceIndex, title, issue, phrase };
  }).filter(({ issue, phrase }) => Boolean(issue || phrase));
}

function CareerAdviceList({ values }: { values: Array<AdminUsageCareerElementAdvice | string> }) {
  const { translate } = useLanguage();
  return (
    <div className="admin-usage-advice-list">
      {values.map((value, index) => typeof value === 'string' ? (
        <p key={`${index}-${value}`}>{value}</p>
      ) : (
        <article key={`${index}-${value.element || value.suggestion || ''}`}>
          {value.element && <h5>{value.element}</h5>}
          {value.suggestion && <p><strong>{translate('建议', 'Suggestion')}</strong>{value.suggestion}</p>}
          {value.reason && <p><strong>{translate('原因', 'Reason')}</strong>{value.reason}</p>}
        </article>
      ))}
    </div>
  );
}

function CoachTaskCard({ task }: { task: AdminUsageCoachTask }) {
  const { translate } = useLanguage();
  const canonicalTitle = COACH_TASK_TITLES[task.task_id];
  const taskTitle = canonicalTitle
    ? translate(canonicalTitle[0], canonicalTitle[1])
    : task.task_name || task.task_id;
  const sections = [
    [translate('评分依据', 'Scoring basis'), task.basis || []],
    [translate('做得较好', 'Strengths'), task.strengths || []],
    [translate('风险提示', 'Risks'), (task.risks || []).map(renderRisk).filter(Boolean)],
  ] as const;
  const improvementRows = buildAdminImprovementRows(task, translate);
  return (
    <article className="admin-usage-coach-task">
      <header>
        <div>
          <span>{taskStatusLabel(task.status, translate)}</span>
          <h3>{taskTitle}</h3>
        </div>
        {typeof task.score === 'number' && <strong>{task.score}<small>/5</small></strong>}
      </header>
      {task.summary && <p className="admin-usage-task-summary">{task.summary}</p>}
      {sections.map(([title, values]) => values.length > 0 && (
        <section key={title}>
          <h4>{title}</h4>
          <StringList values={values} />
        </section>
      ))}
      {improvementRows.length > 0 && (
        <section
          className="dimension-report-body"
          aria-label={translate('需要改进与具体建议', 'Improvements and specific suggestions')}
        >
          <div className="improvement-comparison-header" aria-hidden="true">
            <div className="dimension-findings improvement-column-heading">
              <div className="dimension-section-title">
                <h4>{translate('需要改进', 'Needs improvement')}</h4>
              </div>
            </div>
            <div className="dimension-actions improvement-column-heading">
              <div className="dimension-section-title">
                <h4>{translate('具体建议', 'Specific suggestion')}</h4>
              </div>
            </div>
          </div>
          <ol className="improvement-comparison-list">
            {improvementRows.map(({ sourceIndex, title, issue, phrase }) => (
              <li
                className="improvement-comparison-row"
                key={`${sourceIndex}-${title}-${issue || phrase?.suggestion || 'improvement'}`}
              >
                <section
                  className="dimension-findings"
                  aria-label={translatedTemplate(
                    translate,
                    '{title}：需要改进',
                    '{title}: needs improvement',
                    { title },
                  )}
                >
                  <span className="improvement-cell-label">
                    {translate('需要改进', 'Needs improvement')}
                  </span>
                  <strong className="improvement-row-title">{title}</strong>
                  <div className="issue-dimension-copy">
                    {issue || (
                      <span className="dimension-empty">
                        {translate('本项未记录明确的改进重点。', 'No explicit improvement focus was recorded for this item.')}
                      </span>
                    )}
                  </div>
                </section>
                <section
                  className="dimension-actions"
                  aria-label={translatedTemplate(
                    translate,
                    '{title}：具体建议',
                    '{title}: specific suggestion',
                    { title },
                  )}
                >
                  <span className="improvement-cell-label">
                    {translate('具体建议', 'Specific suggestion')}
                  </span>
                  {phrase?.original && (
                    <p className="original-phrase">
                      <span>{translate('经理实际表达', 'Manager’s exact wording')}</span>
                      {phrase.original}
                    </p>
                  )}
                  {phrase?.suggestion ? (
                    <div className="suggested-phrase">
                      <ArrowRight size={17} aria-hidden="true" />
                      <strong>{phrase.suggestion}</strong>
                    </div>
                  ) : (
                    <p className="dimension-empty">
                      {translate('本项未返回可执行的具体建议。', 'No actionable suggestion was returned for this item.')}
                    </p>
                  )}
                  {phrase?.reason && <p className="improvement-reason">{phrase.reason}</p>}
                </section>
              </li>
            ))}
          </ol>
        </section>
      )}
      {(task.career_elements_advice || []).length > 0 && (
        <section>
          <h4>Career Elements</h4>
          <CareerAdviceList values={task.career_elements_advice || []} />
        </section>
      )}
    </article>
  );
}

function ReportPanel({ tasks }: { tasks: AdminUsageCoachTask[] | null | undefined }) {
  const { translate } = useLanguage();
  if (!tasks?.length) {
    return <EmptyDetail text={translate('本次会话未生成复盘报告。', 'No review report was generated for this session.')} />;
  }
  return <div className="admin-usage-report-list">{tasks.map((task) => <CoachTaskCard key={task.task_id} task={task} />)}</div>;
}

export function AdminUsageSessionViewer({
  detail,
  activeSection,
  onSectionChange,
}: AdminUsageSessionViewerProps) {
  const { translate } = useLanguage();
  const tabListRef = useRef<HTMLElement>(null);
  const bodyRef = useRef<HTMLDivElement>(null);
  const previousSectionRef = useRef(activeSection);
  const tabs = useMemo(() => {
    const guidance = detail.guidance;
    const guidanceCount = guidance
      ? Number(Boolean(guidance.purpose))
        + Number(Boolean(guidance.opening_suggestion))
        + Object.values(guidance.dimension_points || {})
          .reduce((total, groups) => total + (groups?.length || 0), 0)
        + (guidance.risk_preview?.length || 0)
        + (guidance.response_strategies?.length || 0)
        + (guidance.safer_phrases?.length || 0)
      : 0;
    const rehearsalCount = (detail.conversation || [])
      .filter((turn) => turn.text.trim())
      .length;
    const reportCount = detail.coach_tasks?.length || 0;
    const countStatus = (
      count: number,
      singularZh: string,
      pluralZh: string,
      singularEn: string,
      pluralEn: string,
      emptyZh: string,
      emptyEn: string,
    ) => (
      count > 0
        ? translatedTemplate(
          translate,
          count === 1 ? singularZh : pluralZh,
          count === 1 ? singularEn : pluralEn,
          { count },
        )
        : translate(emptyZh, emptyEn)
    );
    return [
      {
        id: 'overview' as const,
        label: translate('概览', 'Overview'),
        status: translate('可用', 'Available'),
        icon: UserRound,
      },
      {
        id: 'guidance' as const,
        label: translate('谈前指导', 'Guidance'),
        status: countStatus(
          guidanceCount,
          '共 {count} 项',
          '{count} 项',
          '{count} item',
          '{count} items',
          '未生成',
          'Not generated',
        ),
        icon: ClipboardList,
      },
      {
        id: 'rehearsal' as const,
        label: translate('预演对话', 'Rehearsal'),
        status: countStatus(
          rehearsalCount,
          '共 {count} 条',
          '{count} 条',
          '{count} turn',
          '{count} turns',
          '无对话',
          'No turns',
        ),
        icon: MessageSquareText,
      },
      {
        id: 'report' as const,
        label: translate('复盘报告', 'Review'),
        status: countStatus(
          reportCount,
          '共 {count} 个维度',
          '{count} 个维度',
          '{count} dimension',
          '{count} dimensions',
          '未生成',
          'Not generated',
        ),
        icon: CheckCircle2,
      },
    ];
  }, [detail, translate]);

  useEffect(() => {
    const sectionChanged = previousSectionRef.current !== activeSection;
    previousSectionRef.current = activeSection;
    const frame = window.requestAnimationFrame(() => {
      const body = bodyRef.current;
      if (sectionChanged && body) {
        const overflowY = window.getComputedStyle(body).overflowY;
        const bodyScrolls = overflowY !== 'visible'
          && body.scrollHeight > body.clientHeight + 1;
        if (bodyScrolls) body.scrollTo({ top: 0, left: 0, behavior: 'auto' });
        else tabListRef.current?.scrollIntoView({ block: 'start', behavior: 'auto' });
      }
      tabListRef.current
        ?.querySelector<HTMLElement>(`#admin-usage-tab-${activeSection}`)
        ?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [activeSection]);

  const handleTabKeyDown = (
    event: ReactKeyboardEvent<HTMLButtonElement>,
    currentIndex: number,
  ) => {
    let nextIndex: number;
    if (event.key === 'ArrowRight') nextIndex = (currentIndex + 1) % tabs.length;
    else if (event.key === 'ArrowLeft') nextIndex = (currentIndex - 1 + tabs.length) % tabs.length;
    else if (event.key === 'Home') nextIndex = 0;
    else if (event.key === 'End') nextIndex = tabs.length - 1;
    else return;

    event.preventDefault();
    const nextSection = tabs[nextIndex].id;
    onSectionChange(nextSection);
    window.requestAnimationFrame(() => {
      tabListRef.current
        ?.querySelector<HTMLButtonElement>(`#admin-usage-tab-${nextSection}`)
        ?.focus();
    });
  };

  return (
    <>
      <nav
        ref={tabListRef}
        className="admin-usage-detail-tabs"
        role="tablist"
        aria-label={translate('使用详情内容', 'Usage detail sections')}
        aria-orientation="horizontal"
      >
        {tabs.map(({ id, label, status, icon: Icon }, index) => (
          <button
            id={`admin-usage-tab-${id}`}
            key={id}
            type="button"
            role="tab"
            aria-selected={activeSection === id}
            aria-controls={`admin-usage-panel-${id}`}
            aria-label={translatedTemplate(
              translate,
              '{label}，{status}',
              '{label}, {status}',
              { label, status },
            )}
            tabIndex={activeSection === id ? 0 : -1}
            onClick={() => onSectionChange(id)}
            onKeyDown={(event) => handleTabKeyDown(event, index)}
          >
            <Icon size={17} aria-hidden="true" />
            <span>{label}</span>
            <small aria-hidden="true">{status}</small>
          </button>
        ))}
      </nav>
      <div
        ref={bodyRef}
        id={`admin-usage-panel-${activeSection}`}
        className="admin-usage-detail-body"
        role="tabpanel"
        aria-labelledby={`admin-usage-tab-${activeSection}`}
        tabIndex={0}
      >
        {activeSection === 'overview' && <OverviewPanel detail={detail} />}
        {activeSection === 'guidance' && <GuidancePanel guidance={detail.guidance} />}
        {activeSection === 'rehearsal' && <ConversationPanel detail={detail} />}
        {activeSection === 'report' && <ReportPanel tasks={detail.coach_tasks} />}
      </div>
    </>
  );
}

export function AdminUsageSessionDialog({
  conversation,
  detail,
  loading,
  error,
  onClose,
  onRetry,
  onOpenPage,
}: AdminUsageSessionDialogProps) {
  const { language, translate } = useLanguage();
  const [searchParams] = useSearchParams();
  const [activeSection, setActiveSection] = useState<AdminUsageSection>('overview');
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useDialogFocus<HTMLElement, HTMLButtonElement>({
    active: true,
    initialFocusRef: closeButtonRef,
    onClose,
  });

  const displayName = detail?.user_display_name
    || conversation.user_display_name
    || conversation.user_email.split('@')[0];

  return (
    <div
      className="modal-backdrop admin-usage-detail-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <section
        ref={dialogRef}
        className="admin-usage-detail-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="admin-usage-detail-title"
        aria-describedby="admin-usage-detail-description"
        tabIndex={-1}
      >
        <header className="admin-usage-detail-header">
          <div>
            <span className="admin-usage-read-only-badge">
              <FileText size={14} aria-hidden="true" />
              {translate('只读记录', 'Read-only record')}
            </span>
            <h2 id="admin-usage-detail-title">{translate('用户使用详情', 'User usage details')}</h2>
            <p id="admin-usage-detail-description">
              <strong>{displayName}</strong>
              <span>{conversation.user_email}</span>
              <span>{displayDate(conversation.conversation_started_at || conversation.session_created_at, language, translate)}</span>
            </p>
          </div>
          <div className="admin-usage-detail-header-actions">
            <Link
              className="admin-usage-detail-open-page"
              to={adminUsageSessionPath(conversation.session_id, activeSection, searchParams)}
              title={translate('进入会话页面', 'Open session page')}
              onClick={onOpenPage}
            >
              <ExternalLink size={17} aria-hidden="true" />
              <span>{translate('进入会话页面', 'Open session page')}</span>
            </Link>
            <button
              ref={closeButtonRef}
              className="admin-usage-detail-close"
              type="button"
              onClick={onClose}
              title={translate('关闭详情', 'Close details')}
              aria-label={translate('关闭用户使用详情', 'Close user usage details')}
            >
              <X size={20} aria-hidden="true" />
            </button>
          </div>
        </header>

        {loading ? (
          <div className="admin-usage-detail-loading" role="status" aria-live="polite">
            <LoaderCircle className="admin-loading-icon" size={26} aria-hidden="true" />
            <p>{translate('正在读取这次使用记录…', 'Loading this usage record…')}</p>
          </div>
        ) : error ? (
          <div className="admin-usage-detail-error" role="alert">
            <AlertTriangle size={27} aria-hidden="true" />
            <h3>{translate('无法读取使用详情', 'Unable to load usage details')}</h3>
            <p>{error}</p>
            <button className="btn btn-secondary" type="button" onClick={onRetry}>
              <RefreshCw size={16} aria-hidden="true" />
              {translate('重新加载', 'Reload')}
            </button>
          </div>
        ) : detail ? (
          <AdminUsageSessionViewer
            detail={detail}
            activeSection={activeSection}
            onSectionChange={setActiveSection}
          />
        ) : (
          <EmptyDetail text={translate('没有可展示的使用详情。', 'No usage details are available.')} />
        )}
      </section>
    </div>
  );
}

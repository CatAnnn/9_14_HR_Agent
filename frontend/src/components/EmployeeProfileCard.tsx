import { useEffect, useRef, useState, type ReactNode } from 'react';
import { useLanguage } from '../i18n/LanguageContext';
import {
  parseProfileGoalDimensions,
  profileGoalSystemLabelEnglish,
  type ProfileGoalDimension,
  type ProfileGoalPoint,
} from '../utils/profileGoals';

export type EmployeeProfileRow = readonly [icon: string, label: string, value: string];

const PROFILE_UI_ENGLISH: Readonly<Record<string, string>> = {
  '员工档案': 'Employee profile',
  '员工档案预览': 'Employee profile preview',
  '暂无员工档案信息。': 'No employee profile information is available.',
  '工号': 'Employee ID',
  '姓名': 'Name',
  '员工代称': 'Employee alias',
  '岗位': 'Role',
  '职位': 'Role',
  '部门': 'Department',
  '职级': 'Level',
  '岗级': 'Level',
  '当前绩效评级': 'Current performance rating',
  '绩效评级': 'Performance rating',
  '绩效': 'Performance',
  '汇报关系': 'Reporting line',
  '考核周期': 'Review cycle',
  '本次谈话主题': 'Conversation topic',
  '关键目标': 'Key goals',
  '完成情况': 'Completion status',
  '历史反馈': 'Previous feedback',
  '管理动作': 'Management actions',
  '员工状态': 'Employee status',
  '敏感约束': 'Sensitive constraints',
  '额外提供的信息': 'Additional information',
  '当前员工目标': 'Current employee goals',
  '沟通意图': 'Conversation intent',
  '员工的主诉求': 'Primary employee motive',
  '员工的辅诉求': 'Secondary employee motives',
  '本次会话补充': 'Additional session context',
  '本次沟通': 'This conversation',
  '沟通与诉求': 'Conversation and motives',
};

function profileUiEnglish(value: string): string {
  return PROFILE_UI_ENGLISH[value] || value;
}

interface EmployeeProfileCardProps {
  title?: string;
  rows: ReadonlyArray<EmployeeProfileRow>;
  emptyText?: string;
  hideRowLabels?: boolean;
  hideRowLabelKeys?: ReadonlyArray<string>;
  contextTitle?: string;
  contextRows?: ReadonlyArray<EmployeeProfileRow>;
  goalSource?: unknown;
  className?: string;
  children?: ReactNode;
}

function GoalPointList({ points }: { points: ProfileGoalPoint[] }) {
  if (!points.length) return null;
  return (
    <ul className="profile-goal-points">
      {points.map((point, index) => (
        <li className="profile-goal-point" key={`${index}-${point.marker || ''}-${point.text}`}>
          {point.markerKind === 'number' ? (
            <span className="profile-goal-point-marker">{point.marker}</span>
          ) : (
            <span className="profile-goal-point-marker is-dot" aria-hidden="true" />
          )}
          <span className="profile-goal-point-text">{point.text}</span>
        </li>
      ))}
    </ul>
  );
}

function GoalDimensions({
  dimensions,
  label,
}: {
  dimensions: ProfileGoalDimension[];
  label: string;
}) {
  const { translate, translateTemplate } = useLanguage();
  const displaySystemLabel = (value: string) => {
    const english = profileGoalSystemLabelEnglish(value);
    return english ? translate(value, english) : value;
  };
  return (
    <div
      className="profile-row-value profile-goal-dimensions"
      role="list"
      aria-label={translateTemplate('{label}维度', '{label} dimensions', { label })}
    >
      {dimensions.map((dimension, dimensionIndex) => (
        <section
          className="profile-goal-dimension"
          role="listitem"
          key={`${dimensionIndex}-${dimension.title}`}
        >
          <h3 className="profile-goal-dimension-title">{displaySystemLabel(dimension.title)}</h3>
          {dimension.groups.length > 0 && (
            <div className="profile-goal-groups">
              {dimension.groups.map((group, groupIndex) => (
                <div
                  className={`profile-goal-group${group.title ? ' has-title' : ''}`}
                  key={`${groupIndex}-${group.title || 'items'}`}
                >
                  {group.title && (
                    <h4 className="profile-goal-group-title">{displaySystemLabel(group.title)}</h4>
                  )}
                  <GoalPointList points={group.points} />
                </div>
              ))}
            </div>
          )}
        </section>
      ))}
    </div>
  );
}

function ProfileRow({
  row,
  hideLabel = false,
  goalSource,
}: {
  row: EmployeeProfileRow;
  hideLabel?: boolean;
  goalSource?: unknown;
}) {
  const { language, translate, translateTemplate } = useLanguage();
  const [rowKey, label, value] = row;
  const displayLabel = translate(label, profileUiEnglish(label));
  const displayValue = language !== 'zh-CN' && rowKey === 'PF'
    ? value.replace(/(^|\s)评级\s*/gu, `$1${translate('评级', 'Rating')} `)
    : value;
  const lines = displayValue.split('\n').map((line) => line.trim()).filter(Boolean);
  const isMultiline = lines.length > 1;
  const isGoalRow = rowKey === 'GO' || label.includes('目标');
  const goalDimensions = isGoalRow && goalSource != null
    ? parseProfileGoalDimensions(goalSource)
    : [];
  const isDossierColumnRow = ['NM', 'DP', 'LV', 'PF'].includes(rowKey);
  const isRoleRow = rowKey === 'RL' && label === '岗位';
  const isPerformanceRow = rowKey === 'PF' || label === '当前绩效评级';
  const bulletLines = lines
    .map((line) => line.replace(/^(?:[-*•·▪◦]\s*|\d+[.)、]\s*)/, '').trim())
    .filter(Boolean);
  const displayedBulletLines = bulletLines.length ? bulletLines : [displayValue];
  const classes = [
    'profile-row',
    isGoalRow ? 'is-goal-row' : '',
    goalDimensions.length ? 'has-goal-dimensions' : '',
    isDossierColumnRow ? 'is-dossier-column-row' : '',
    isRoleRow ? 'is-role-row' : '',
    isPerformanceRow ? 'is-performance-row' : '',
    hideLabel ? 'is-label-hidden' : '',
  ].filter(Boolean).join(' ');

  return (
    <div className={classes} data-profile-row-key={rowKey}>
      <span className={hideLabel ? 'sr-only' : 'profile-row-label'}>{displayLabel}</span>
      {isGoalRow && goalDimensions.length > 0 ? (
        <GoalDimensions dimensions={goalDimensions} label={displayLabel} />
      ) : isGoalRow ? (
        <ul
          className="profile-row-value profile-row-bullets"
          aria-label={translateTemplate('{label}列表', '{label} list', { label: displayLabel })}
        >
          {displayedBulletLines.map((line, index) => <li key={`${index}-${line}`}>{line}</li>)}
        </ul>
      ) : (
        <strong className={isMultiline ? 'profile-row-value is-multiline' : 'profile-row-value'}>
          {isPerformanceRow
            ? <span className="rating-pill">{lines[0] || displayValue}</span>
            : isMultiline
              ? (
                <span className="profile-row-lines">
                  {lines.map((line, index) => <span key={`${index}-${line}`}>{line}</span>)}
                </span>
              )
              : lines[0] || displayValue}
        </strong>
      )}
    </div>
  );
}

export default function EmployeeProfileCard({
  title = '员工档案',
  rows,
  emptyText = '暂无员工档案信息。',
  hideRowLabels = false,
  hideRowLabelKeys = [],
  contextTitle,
  contextRows = [],
  goalSource,
  className = '',
  children,
}: EmployeeProfileCardProps) {
  const { translate } = useLanguage();
  const displayTitle = translate(title, profileUiEnglish(title));
  const displayEmptyText = translate(emptyText, profileUiEnglish(emptyText));
  const displayContextTitle = contextTitle
    ? translate(contextTitle, profileUiEnglish(contextTitle))
    : undefined;
  const [isScrolling, setIsScrolling] = useState(false);
  const [isScrollFading, setIsScrollFading] = useState(false);
  const scrollEndTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const scrollFadeTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const classes = [
    'soft-card',
    'profile-card',
    'summary-panel',
    'employee-profile-card',
    isScrolling ? 'is-scrolling' : '',
    isScrollFading ? 'is-scroll-fading' : '',
    className,
  ]
    .filter(Boolean)
    .join(' ');
  const hasContext = Boolean(contextTitle || contextRows.length || children);

  useEffect(() => () => {
    if (scrollEndTimerRef.current !== null) {
      clearTimeout(scrollEndTimerRef.current);
    }
    if (scrollFadeTimerRef.current !== null) {
      clearTimeout(scrollFadeTimerRef.current);
    }
  }, []);

  const handleScroll = () => {
    setIsScrolling(true);
    setIsScrollFading(false);
    if (scrollEndTimerRef.current !== null) {
      clearTimeout(scrollEndTimerRef.current);
    }
    if (scrollFadeTimerRef.current !== null) {
      clearTimeout(scrollFadeTimerRef.current);
      scrollFadeTimerRef.current = null;
    }
    scrollEndTimerRef.current = setTimeout(() => {
      setIsScrolling(false);
      setIsScrollFading(true);
      scrollEndTimerRef.current = null;
      scrollFadeTimerRef.current = setTimeout(() => {
        setIsScrollFading(false);
        scrollFadeTimerRef.current = null;
      }, 320);
    }, 560);
  };

  return (
    <section className={classes} onScroll={handleScroll}>
      <div className="card-title employee-profile-card-title">
        <div><h2>{displayTitle}</h2></div>
      </div>

      <div className="profile-preview">
        {!rows.length && displayEmptyText && <div className="empty-state">{displayEmptyText}</div>}
        {rows.map((row) => (
          <ProfileRow
            key={row[0]}
            row={row}
            hideLabel={hideRowLabels || hideRowLabelKeys.includes(row[0])}
            goalSource={row[0] === 'GO' ? goalSource : undefined}
          />
        ))}
      </div>

      {hasContext && (
        <div className="employee-profile-context">
          {displayContextTitle && <h3 className="employee-profile-context-title">{displayContextTitle}</h3>}
          {contextRows.length > 0 && (
            <div className="profile-context-rows">
              {contextRows.map((row) => <ProfileRow key={row[1]} row={row} />)}
            </div>
          )}
          {children && <div className="profile-detail-sections">{children}</div>}
        </div>
      )}
    </section>
  );
}

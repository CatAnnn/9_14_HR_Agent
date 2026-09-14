import {
  ArrowUp,
  Check,
  CircleStop,
  LoaderCircle,
  Mic,
  Plus,
  Trash2,
  X,
} from 'lucide-react';
import {
  memo,
  type RefObject,
  type UIEvent as ReactUIEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import { useNavigate } from 'react-router-dom';
import { STATIC_ASSETS } from '../../config/staticAssets';
import { useWorkflow } from '../../context/WorkflowContext';
import { useSpeechToText } from '../../hooks/useSpeechToText';
import { useStreamingSpeechPlayback } from '../../hooks/useStreamingSpeechPlayback';
import { useLanguage } from '../../i18n/LanguageContext';
import {
  emotionName as localizedEmotionName,
  motiveName as localizedMotiveName,
} from '../../i18n/businessLabels';
import type {
  ConversationTurn,
  EmployeeProfile,
  IntentGoalPerformanceItem,
  RehearsalDimensionId,
} from '../../types/domain';
import {
  buildCompactGuidance,
  type CompactGuidanceSection,
} from '../../utils/compactGuidance';
import { profileValueToText } from '../../utils/format';
import { normalizeDisplayText, userFacingErrorMessage } from '../../utils/displayText';
import {
  parseProfileGoalDimensions,
  profileGoalSource,
  profileGoalSystemLabelEnglish,
} from '../../utils/profileGoals';
import {
  applyEditableSpeechTranscript,
  beginEditableSpeechDraft,
  cancelEditableSpeechDraft,
  editEditableSpeechDraft,
  moveEditableSpeechCaret,
  type EditableSpeechDraft,
} from '../../utils/editableSpeechDraft';
import { shouldPromptEmotionReview } from '../../utils/rehearsalEmotion';
import { speechRecognitionLanguage } from '../../utils/speechRecordingLocale';
import {
  getRehearsalContextModulePosition,
  REHEARSAL_CONTEXT_MODULES,
} from '../../utils/rehearsalContextModules';
import {
  normalizeRuntimeNoteDrafts,
  runtimeNotesToDrafts,
} from '../../utils/rehearsalRuntimeNotes';
import { createRehearsalRequestId } from '../../utils/rehearsalRequest';
import {
  createSerialMessageQueue,
  type SerialMessageQueue,
  type SerialMessageQueueSnapshot,
} from '../../utils/serialMessageQueue';
import {
  activatePushToTalkShortcut,
  cancelPushToTalkShortcut,
  createPushToTalkShortcutState,
  PUSH_TO_TALK_HOLD_MS,
  pushToTalkShortcutKeyDown,
  pushToTalkShortcutKeyUp,
  type PushToTalkShortcutAction,
  type PushToTalkShortcutState,
} from '../../utils/pushToTalkShortcut';

const REHEARSAL_DIMENSION_LABELS_ENGLISH: Readonly<Record<string, string>> = {
  '开场定调': 'Opening alignment',
  '情绪承接': 'Emotional acknowledgment',
  '产出与标准': 'Outcomes and standards',
  '发展计划': 'Development plan',
};

const REHEARSAL_MODULE_LABELS_ENGLISH: Readonly<Record<string, string>> = {
  context: 'Conversation',
  employee: 'Employee',
  goals: 'Goals',
  performance: 'Performance',
  guidance: 'Guidance',
};

const COMPACT_GUIDANCE_LABELS_ENGLISH: Readonly<Record<string, string>> = {
  start: 'Opening and goals',
  emotion: 'Emotion and risks',
  requirement: 'Standards and responses',
  plan: 'Actions and close',
};

const PERFORMANCE_LABELS_ENGLISH: Readonly<Record<string, string>> = {
  '目标达成总览': 'Overall goal attainment',
  '正向表现/取得进展': 'Positive performance / progress made',
  '现存差距与行为实例': 'Current gaps and behavioral examples',
  'Goal Achievement Overview': 'Overall goal attainment',
  'Positive Performance / Progress': 'Positive performance / progress made',
  'Current Gaps and Behavioral Examples': 'Current gaps and behavioral examples',
};

interface ChatMessageProps {
  turn: ConversationTurn;
  isStreaming?: boolean;
  employeeName: string;
}

const ChatMessage = memo(function ChatMessage({ turn, isStreaming, employeeName }: ChatMessageProps) {
  const { translate, translateTemplate } = useLanguage();
  const speaker = turn.speaker || 'system';
  if (speaker === 'system') {
    return (
      <div className="chat-msg system">
        <div className="system-bubble">{normalizeDisplayText(turn.text || '')}</div>
      </div>
    );
  }

  const isManager = speaker === 'manager' || speaker === 'you';
  const visibleText = isManager ? turn.text : normalizeDisplayText(turn.text || '');
  return (
    <div
      className={`chat-msg ${isManager ? 'manager' : 'employee'}`}
      aria-label={isManager
        ? translate('你的消息', 'Your message')
        : translateTemplate(
          '{employee}的消息',
          'Message from {employee}',
          { employee: employeeName || translate('员工', 'employee') },
        )}
    >
      {!isManager && <div className="chat-avatar employee-avatar" aria-hidden="true">{employeeName.slice(0, 1).toUpperCase() || translate('员', 'E')}</div>}
      <div className="chat-content">
        <div className="chat-bubble">
          {visibleText || (isStreaming ? (
            <span className="typing-indicator" role="status" aria-label={translate('正在输入', 'Typing')}>
              <i />
              <i />
              <i />
            </span>
          ) : '')}
        </div>
      </div>
      {isManager && (
        <div className="chat-avatar manager-avatar" aria-hidden="true">
          <img src={STATIC_ASSETS.companyLogo} alt="" />
        </div>
      )}
    </div>
  );
});

interface ChatHistoryProps {
  turns: ConversationTurn[];
  endIndex: number;
  employeeName: string;
}

const ChatHistory = memo(function ChatHistory({
  turns,
  endIndex,
  employeeName,
}: ChatHistoryProps) {
  return turns.slice(0, endIndex).map((turn, index) => (
    <ChatMessage
      key={`${turn.speaker || 'system'}-${index}`}
      turn={turn}
      employeeName={employeeName}
    />
  ));
}, (previous, next) => {
  if (previous.employeeName !== next.employeeName || previous.endIndex !== next.endIndex) return false;
  for (let index = 0; index < next.endIndex; index += 1) {
    if (previous.turns[index] !== next.turns[index]) return false;
  }
  return true;
});

interface ChatListProps {
  turns: ConversationTurn[];
  employeeName: string;
  streaming: boolean;
  listRef: RefObject<HTMLDivElement | null>;
  onScroll: (event: ReactUIEvent<HTMLDivElement>) => void;
}

const ChatList = memo(function ChatList({
  turns,
  employeeName,
  streaming,
  listRef,
  onScroll,
}: ChatListProps) {
  const { translate } = useLanguage();
  const streamingIndex = streaming
    && turns[turns.length - 1]?.speaker === 'employee'
    ? turns.length - 1
    : -1;
  return (
    <div className="chat-list" ref={listRef} onScroll={onScroll}>
      {!turns.length && <div className="chat-empty empty-state">{translate('输入第一句话开始预演。', 'Enter your first message to start the rehearsal.')}</div>}
      <ChatHistory
        turns={turns}
        endIndex={streamingIndex >= 0 ? streamingIndex : turns.length}
        employeeName={employeeName}
      />
      {streamingIndex >= 0 && (
        <ChatMessage
          key={`employee-${streamingIndex}`}
          turn={turns[streamingIndex]}
          employeeName={employeeName}
          isStreaming={!turns[streamingIndex].text}
        />
      )}
    </div>
  );
});

interface RehearsalDimensionStatus {
  id: RehearsalDimensionId;
  label: string;
  active: boolean;
}

const REHEARSAL_DIMENSION_DEFINITIONS: ReadonlyArray<{
  id: RehearsalDimensionId;
  label: string;
}> = [
  { id: 'start', label: '开场定调' },
  { id: 'emotion', label: '情绪承接' },
  { id: 'requirement', label: '产出与标准' },
  { id: 'plan', label: '发展计划' },
];

function getRehearsalDimensionStatuses(
  coveredDimensions: readonly RehearsalDimensionId[],
): RehearsalDimensionStatus[] {
  const covered = new Set(coveredDimensions);
  return REHEARSAL_DIMENSION_DEFINITIONS.map((dimension) => ({
    id: dimension.id,
    label: dimension.label,
    active: covered.has(dimension.id),
  }));
}

interface ConversationContextPanelProps {
  employeeProfile: EmployeeProfile | null;
  employeeName: string;
  primaryMotiveName: string;
  secondaryMotiveNames: string;
  emotionName: string;
  runtimeNotesCount: number;
  onEdit: () => void;
  onReturnToGuidance?: () => void;
  dimensions: RehearsalDimensionStatus[];
  showEmotionReviewPrompt: boolean;
  performanceItems: readonly IntentGoalPerformanceItem[];
  performanceContext: string;
  compactGuidance: readonly CompactGuidanceSection[];
  compactGuidanceState: 'ready' | 'loading' | 'error' | 'unavailable';
  onRetryGuidance?: () => void;
}

const ConversationContextPanel = memo(function ConversationContextPanel({
  employeeProfile,
  employeeName,
  primaryMotiveName,
  secondaryMotiveNames,
  emotionName,
  runtimeNotesCount,
  onEdit,
  onReturnToGuidance,
  dimensions,
  showEmotionReviewPrompt,
  performanceItems,
  performanceContext,
  compactGuidance,
  compactGuidanceState,
  onRetryGuidance,
}: ConversationContextPanelProps) {
  const { translate, translateTemplate } = useLanguage();
  const [activeModuleIndex, setActiveModuleIndex] = useState(0);
  const scrollHideTimersRef = useRef(new Map<HTMLElement, number>());
  const employeeDetails = useMemo(() => {
    const text = (value: unknown, fallback = '—') => profileValueToText(value) || fallback;
    const rating = text(employeeProfile?.performance_rating, '');
    const tcl = text(employeeProfile?.tcl, '');
    const performanceRating = [rating
      ? translateTemplate('评级 {rating}', 'Rating {rating}', { rating })
      : '', tcl ? `TCL ${tcl}` : '']
      .filter(Boolean)
      .join(' / ') || '—';
    const careerElements = text(employeeProfile?.current_career_elements)
      .split('\n')
      .join(' / ');
    return [
      [translate('姓名', 'Name'), text(employeeProfile?.name, employeeName)],
      [translate('工号', 'Employee ID'), text(employeeProfile?.employee_id)],
      [translate('部门', 'Department'), text(employeeProfile?.department)],
      [translate('职位', 'Role'), text(employeeProfile?.role)],
      [translate('岗级', 'Level'), text(employeeProfile?.level)],
      [translate('汇报关系', 'Reporting line'), text(employeeProfile?.reporting_line)],
      [translate('绩效评级', 'Performance rating'), performanceRating],
      ['Career Elements', careerElements],
      [translate('考核周期', 'Review cycle'), text(employeeProfile?.review_cycle)],
      [translate('本次谈话主题', 'Conversation topic'), text(employeeProfile?.conversation_topic)],
    ] as const;
  }, [employeeName, employeeProfile, translate, translateTemplate]);
  const employeeGoalDimensions = useMemo(
    () => parseProfileGoalDimensions(profileGoalSource(employeeProfile)),
    [employeeProfile],
  );
  const displayGoalSystemLabel = (value: string) => {
    const english = profileGoalSystemLabelEnglish(value);
    return english ? translate(value, english) : value;
  };
  const clearScrollActivity = useCallback(() => {
    scrollHideTimersRef.current.forEach((timerId, panel) => {
      window.clearTimeout(timerId);
      panel.classList.remove('is-scrolling');
    });
    scrollHideTimersRef.current.clear();
  }, []);
  const handlePanelScroll = useCallback((event: ReactUIEvent<HTMLElement>) => {
    const panel = event.currentTarget;
    const currentTimer = scrollHideTimersRef.current.get(panel);
    if (currentTimer !== undefined) window.clearTimeout(currentTimer);

    panel.classList.add('is-scrolling');
    const nextTimer = window.setTimeout(() => {
      panel.classList.remove('is-scrolling');
      scrollHideTimersRef.current.delete(panel);
    }, 720);
    scrollHideTimersRef.current.set(panel, nextTimer);
  }, []);
  const selectModule = useCallback((index: number) => {
    clearScrollActivity();
    setActiveModuleIndex((currentIndex) => (
      currentIndex === index ? currentIndex : index
    ));
  }, [clearScrollActivity]);

  useEffect(() => () => clearScrollActivity(), [clearScrollActivity]);

  return (
    <div className="conversation-context-dock">
      <nav className="conversation-module-tabs" aria-label={translate('切换会话信息模块', 'Switch conversation-information section')}>
        {REHEARSAL_CONTEXT_MODULES.map((module, index) => (
          <button
            className={activeModuleIndex === index ? 'is-active' : undefined}
            type="button"
            key={module.id}
            aria-controls={`rehearsal-context-module-${module.id}`}
            aria-current={activeModuleIndex === index ? 'page' : undefined}
            onClick={() => selectModule(index)}
          >
            {translate(module.label, REHEARSAL_MODULE_LABELS_ENGLISH[module.id])}
          </button>
        ))}
      </nav>

      <aside
        className="soft-card conversation-context-panel summary-panel"
        aria-label={translate('会话信息', 'Conversation information')}
      >
        <div
          className="conversation-module-track"
          role="region"
          aria-label={translate('当前会话信息卡片', 'Current conversation-information panel')}
        >
        <section
          id="rehearsal-context-module-context"
          className={`conversation-panel-view conversation-panel-context-view${activeModuleIndex === 0 ? ' is-active' : ''}`}
          data-position={getRehearsalContextModulePosition(0, activeModuleIndex)}
          aria-labelledby="rehearsal-context-module-context-title"
          aria-hidden={activeModuleIndex !== 0}
          inert={activeModuleIndex !== 0}
          onScroll={handlePanelScroll}
        >
          <h2 id="rehearsal-context-module-context-title">{translate('会话信息', 'Conversation information')}</h2>
          <div className="context-facts">
            <div><span>{translate('当前员工', 'Current employee')}</span><strong>{employeeName}</strong></div>
            <div><span>{translate('员工的主诉求', 'Employee primary motive')}</span><strong>{primaryMotiveName}</strong></div>
            <div><span>{translate('员工的辅诉求', 'Employee secondary motives')}</span><strong>{secondaryMotiveNames}</strong></div>
            <div><span>{translate('当前情绪', 'Current emotion')}</span><strong>{emotionName}</strong></div>
            <div className="context-fact-dynamic">
              <span className="context-dynamic-prompt">{translate('是否需要修改或新增当前会话背景？', 'Need to update or add session context?')}</span>
              <button
                className="context-dynamic-setting"
                type="button"
                title={runtimeNotesCount
                  ? translateTemplate(
                    '修改会话背景（已补充 {count} 条）',
                    'Edit session context ({count} added)',
                    { count: runtimeNotesCount },
                  )
                  : translate('修改会话背景', 'Edit session context')}
                onClick={onEdit}
              >
                {translate('修改', 'Edit')}
              </button>
            </div>
          </div>
          <section className="conversation-dimension-status" aria-label={translate('当前对话涉及维度', 'Dimensions covered in the current conversation')}>
            <span className="conversation-dimension-caption">{translate('对话涉及', 'Covered')}</span>
            <div className="conversation-dimension-list" role="list">
              {dimensions.map((dimension) => (
                <span
                  key={dimension.id}
                  className={`conversation-dimension-item${dimension.active ? ' is-active' : ''}`}
                  role="listitem"
                  aria-label={translateTemplate(
                    '{label}，{status}',
                    '{label}, {status}',
                    {
                      label: translate(
                        dimension.label,
                        REHEARSAL_DIMENSION_LABELS_ENGLISH[dimension.label] || dimension.label,
                      ),
                      status: dimension.active
                        ? translate('已涉及', 'covered')
                        : translate('尚未涉及', 'not yet covered'),
                    },
                  )}
                >
                  {translate(dimension.label, REHEARSAL_DIMENSION_LABELS_ENGLISH[dimension.label])}
                </span>
              ))}
            </div>
          </section>
          {showEmotionReviewPrompt && (
            <aside className="conversation-emotion-review-prompt" role="status">
              <strong>{translate('员工情绪连续三轮为负面', 'Employee emotion has been negative for three consecutive turns')}</strong>
              <p>{translate(
                '建议先回顾情绪承接，确认员工的感受与顾虑，再继续推进事实或行动计划。',
                'Review your emotional acknowledgment first. Confirm the employee’s feelings and concerns before moving on to facts or an action plan.',
              )}</p>
            </aside>
          )}
        </section>

        <section
          id="rehearsal-context-module-employee"
          className={`conversation-panel-view${activeModuleIndex === 1 ? ' is-active' : ''}`}
          data-position={getRehearsalContextModulePosition(1, activeModuleIndex)}
          aria-labelledby="rehearsal-context-module-employee-title"
          aria-hidden={activeModuleIndex !== 1}
          inert={activeModuleIndex !== 1}
          onScroll={handlePanelScroll}
        >
          <h2 id="rehearsal-context-module-employee-title">{translate('详细员工信息', 'Detailed employee information')}</h2>
          <div className="context-facts conversation-employee-facts">
            {employeeDetails.map(([label, value]) => (
              <div key={label}><span>{label}</span><strong>{value}</strong></div>
            ))}
          </div>
        </section>

        <section
          id="rehearsal-context-module-goals"
          className={`conversation-panel-view${activeModuleIndex === 2 ? ' is-active' : ''}`}
          data-position={getRehearsalContextModulePosition(2, activeModuleIndex)}
          aria-labelledby="rehearsal-context-module-goals-title"
          aria-hidden={activeModuleIndex !== 2}
          inert={activeModuleIndex !== 2}
          onScroll={handlePanelScroll}
        >
          <h2 id="rehearsal-context-module-goals-title">{translate('当前员工目标', 'Current employee goals')}</h2>
          {employeeGoalDimensions.length > 0 ? (
            <div className="conversation-employee-goals" role="list">
              {employeeGoalDimensions.map((dimension, dimensionIndex) => (
                <section
                  className="conversation-employee-goal-dimension"
                  role="listitem"
                  key={`${dimensionIndex}-${dimension.title}`}
                >
                  <h3>{displayGoalSystemLabel(dimension.title)}</h3>
                  {dimension.groups.map((group, groupIndex) => (
                    <div
                      className="conversation-employee-goal-group"
                      key={`${groupIndex}-${group.title || '目标明细'}`}
                    >
                      {group.title && <h4>{displayGoalSystemLabel(group.title)}</h4>}
                      {group.points.length > 0 && (
                        <ul>
                          {group.points.map((point, pointIndex) => (
                            <li key={`${pointIndex}-${point.marker || ''}-${point.text}`}>
                              {point.marker && <span aria-hidden="true">{point.marker}</span>}
                              <p className={point.marker ? undefined : 'is-full-width'}>{point.text}</p>
                            </li>
                          ))}
                        </ul>
                      )}
                    </div>
                  ))}
                </section>
              ))}
            </div>
          ) : (
            <div className="conversation-module-empty">{translate('暂无当前员工目标信息。', 'No current employee goals are available.')}</div>
          )}
        </section>

        <section
          id="rehearsal-context-module-performance"
          className={`conversation-panel-view${activeModuleIndex === 3 ? ' is-active' : ''}`}
          data-position={getRehearsalContextModulePosition(3, activeModuleIndex)}
          aria-labelledby="rehearsal-context-module-performance-title"
          aria-hidden={activeModuleIndex !== 3}
          inert={activeModuleIndex !== 3}
          onScroll={handlePanelScroll}
        >
          <h2 id="rehearsal-context-module-performance-title">{translate('当前员工表现', 'Current employee performance')}</h2>
          <div className="conversation-performance-list" aria-live="polite">
            {performanceItems.length > 0 ? (
              performanceItems.map((item, index) => (
                <section
                  className="conversation-performance-item"
                  key={`${item.goal}-${index}`}
                >
                  <h3>{translate(
                    normalizeDisplayText(item.goal) || `表现维度 ${index + 1}`,
                    PERFORMANCE_LABELS_ENGLISH[normalizeDisplayText(item.goal)] || `Performance dimension ${index + 1}`,
                  )}</h3>
                  <p>{normalizeDisplayText(item.current_performance) || translate('暂无当前表现记录。', 'No current performance record is available.')}</p>
                </section>
              ))
            ) : performanceContext.trim() ? (
              <div className="conversation-performance-fallback">
                <p>{normalizeDisplayText(performanceContext)}</p>
              </div>
            ) : (
              <div className="conversation-module-empty">{translate('暂无当前员工表现信息。', 'No current employee performance information is available.')}</div>
            )}
          </div>
        </section>

        <section
          id="rehearsal-context-module-guidance"
          className={`conversation-panel-view conversation-guidance-view${activeModuleIndex === 4 ? ' is-active' : ''}`}
          data-position={getRehearsalContextModulePosition(4, activeModuleIndex)}
          aria-labelledby="rehearsal-context-module-guidance-title"
          aria-hidden={activeModuleIndex !== 4}
          inert={activeModuleIndex !== 4}
          onScroll={handlePanelScroll}
        >
          <h2 id="rehearsal-context-module-guidance-title">{translate('谈前指导', 'Preparation guidance')}</h2>
          {compactGuidanceState === 'ready' ? (
            <div className="conversation-guidance-summary">
              {compactGuidance.map((section) => (
                <section className="conversation-guidance-section" key={section.id}>
                  <h3>{translate(section.title, COMPACT_GUIDANCE_LABELS_ENGLISH[section.id])}</h3>
                  <ul>
                    {section.points.map((point, index) => (
                      <li key={`${section.id}-${index}-${point}`}>{point}</li>
                    ))}
                  </ul>
                </section>
              ))}
            </div>
          ) : (
            <div className="conversation-module-empty" role="status">
              {compactGuidanceState === 'loading'
                ? translate('正在载入已生成的谈前指导…', 'Loading generated preparation guidance…')
                : compactGuidanceState === 'error'
                  ? translate('谈前指导暂时载入失败，请重试。', 'Preparation guidance could not be loaded. Please try again.')
                  : translate('当前会话还没有可展示的谈前指导。', 'No preparation guidance is available for this session yet.')}
              {compactGuidanceState === 'error' && onRetryGuidance && (
                <button
                  className="conversation-guidance-retry"
                  type="button"
                  onClick={onRetryGuidance}
                >
                  {translate('重新载入', 'Reload')}
                </button>
              )}
            </div>
          )}
          {onReturnToGuidance && (
            <div className="conversation-guidance-footer">
              <button
                className="rehearsal-guidance-return"
                type="button"
                title={translate('查看完整谈前指导', 'View full preparation guidance')}
                onClick={onReturnToGuidance}
              >
                <span>{translate('查看完整谈前指导', 'View full preparation guidance')}</span>
              </button>
            </div>
          )}
        </section>
        </div>

        <span className="sr-only" aria-live="polite">
          {translate('当前显示：', 'Currently showing: ')}
          {translate(
            REHEARSAL_CONTEXT_MODULES[activeModuleIndex].label,
            REHEARSAL_MODULE_LABELS_ENGLISH[REHEARSAL_CONTEXT_MODULES[activeModuleIndex].id],
          )}
        </span>
      </aside>
    </div>
  );
});

interface RehearsalStepProps {
  reportPath?: string;
  guidancePath?: string | null;
}

interface QueuedManagerMessage {
  requestId: string;
  text: string;
  sessionId: string | null;
  speechEnabled: boolean;
  voice: string;
}

const EMPTY_MESSAGE_QUEUE: SerialMessageQueueSnapshot = {
  active: false,
  paused: false,
  size: 0,
  waiting: 0,
};

export default function RehearsalStep({
  reportPath = '/app/report',
  guidancePath = '/app/guidance',
}: RehearsalStepProps) {
  const navigate = useNavigate();
  const { language, translate, translateTemplate } = useLanguage();
  const {
    session,
    guidanceReport,
    guidanceSections,
    guidanceStatus,
    ensureGuidance,
    options,
    liveConversation,
    rehearsalStreaming,
    rehearsalDraft: message,
    setRehearsalDraft: setMessage,
    sendMessage,
    ensureSessionLocale,
    updateRehearsalContext,
    endRehearsal,
    showToast,
  } = useWorkflow();
  const [contextOpen, setContextOpen] = useState(false);
  const [runtimeNoteDrafts, setRuntimeNoteDrafts] = useState<string[]>(['']);
  const [pushToTalkShortcut, setPushToTalkShortcut] = useState<PushToTalkShortcutState>(
    () => createPushToTalkShortcutState(true),
  );
  const [sendAfterSpeechFinalizes, setSendAfterSpeechFinalizes] = useState(false);
  const guidanceRestoreAttemptRef = useRef<string | null>(null);
  const chatListRef = useRef<HTMLDivElement | null>(null);
  const shouldFollowChatTailRef = useRef(true);
  const chatScrollTimerRef = useRef<number | null>(null);
  const lastChatScrollAtRef = useRef(0);
  const messageInputRef = useRef<HTMLTextAreaElement | null>(null);
  const messageInputMaxHeightRef = useRef<number | null>(null);
  const messageRef = useRef(message);
  const speechDraftRef = useRef<EditableSpeechDraft | null>(null);
  const speechOptionsHydratedRef = useRef(false);
  const queueSessionIdRef = useRef<string | null>(session?.session_id || null);
  const queueMountedRef = useRef(true);
  const contextOpenRef = useRef(contextOpen);
  const pushToTalkShortcutRef = useRef(pushToTalkShortcut);
  const startSpeechInputRef = useRef<() => boolean>(() => false);
  const stopSpeechInputRef = useRef<() => void>(() => undefined);
  const cancelSpeechInputRef = useRef<() => void>(() => undefined);
  const activateSpeechHoldRef = useRef<() => void>(() => undefined);
  const insertOrdinarySpaceRef = useRef<() => void>(() => undefined);
  const pushToTalkHoldTimerRef = useRef<number | null>(null);
  const pendingSpaceSelectionRef = useRef<{ start: number; end: number } | null>(null);
  const speechInputStateRef = useRef({ requesting: false, recording: false, transcribing: false });
  const sendAfterSpeechFinalizesRef = useRef(false);
  const queueWorkerRef = useRef<(item: QueuedManagerMessage) => Promise<boolean>>(async () => false);
  const [messageQueueSnapshot, setMessageQueueSnapshot] = useState<SerialMessageQueueSnapshot>(EMPTY_MESSAGE_QUEUE);
  const messageQueueRef = useRef<SerialMessageQueue<QueuedManagerMessage> | null>(null);
  if (!messageQueueRef.current) {
    messageQueueRef.current = createSerialMessageQueue(
      (item) => queueWorkerRef.current(item),
      (snapshot) => {
        if (queueMountedRef.current) setMessageQueueSnapshot(snapshot);
      },
    );
  }
  const messageQueue = messageQueueRef.current;
  const speechPlayback = useStreamingSpeechPlayback();
  const lastSpeechPlaybackErrorRef = useRef<string | null>(null);
  const [speechOutputEnabled, setSpeechOutputEnabled] = useState(true);
  const speechOptions = options.speech;
  const speechServiceEnabled = speechOptions?.enabled === true;
  const selectedSpeechVoice = speechOptions?.default_voice || '';
  messageRef.current = message;
  contextOpenRef.current = contextOpen;
  pushToTalkShortcutRef.current = pushToTalkShortcut;

  const commitMessage = useCallback((next: string) => {
    if (messageRef.current === next) return false;
    messageRef.current = next;
    setMessage(next);
    return true;
  }, [setMessage]);

  const processQueuedMessage = useCallback(async (item: QueuedManagerMessage) => {
    if (item.sessionId && item.sessionId !== (session?.session_id || null)) {
      showToast('会话已变化，未发送的追加消息已暂停，请确认后重试。', 'error');
      return false;
    }
    await ensureSessionLocale();
    let speechStreamId: string | null = null;
    if (item.speechEnabled) {
      speechStreamId = await speechPlayback.prepare(item.sessionId);
    } else {
      speechPlayback.stop();
    }
    return await sendMessage(
      item.text,
      {
        enabled: Boolean(speechStreamId),
        voice: speechStreamId ? item.voice : null,
        stream_id: speechStreamId,
      },
      speechPlayback.handleEvent,
      item.requestId,
    );
  }, [
    ensureSessionLocale,
    sendMessage,
    session?.session_id,
    speechPlayback,
    showToast,
    translate,
  ]);

  queueWorkerRef.current = processQueuedMessage;

  const enqueuePreparedMessage = useCallback((text: string) => {
    const normalized = text.trim();
    if (!normalized) return;
    speechDraftRef.current = null;
    commitMessage('');
    messageQueue.enqueue({
      requestId: createRehearsalRequestId(),
      text: normalized,
      sessionId: session?.session_id || null,
      speechEnabled: speechOutputEnabled && speechServiceEnabled,
      voice: selectedSpeechVoice,
    });
  }, [
    commitMessage,
    messageQueue,
    selectedSpeechVoice,
    session?.session_id,
    speechOutputEnabled,
    speechServiceEnabled,
  ]);

  const applySpeechTranscript = useCallback((text: string) => {
    const current = speechDraftRef.current;
    if (!current) return null;
    const input = messageInputRef.current;
    const shouldFollowTail = Boolean(
      input
      && input.selectionStart === current.value.length
      && input.selectionEnd === current.value.length,
    );
    const next = applyEditableSpeechTranscript(current, text);
    speechDraftRef.current = next;
    if (commitMessage(next.value)) {
      window.requestAnimationFrame(() => {
        const currentInput = messageInputRef.current;
        if (currentInput && shouldFollowTail) currentInput.scrollTop = currentInput.scrollHeight;
      });
    }
    return next.value;
  }, [commitMessage]);
  const clearDeferredSpeechSend = useCallback(() => {
    sendAfterSpeechFinalizesRef.current = false;
    setSendAfterSpeechFinalizes(false);
  }, []);
  const handleSpeechTranscript = useCallback((text: string) => {
    const composed = applySpeechTranscript(text);
    if (composed === null) return;
    speechDraftRef.current = null;
    if (sendAfterSpeechFinalizesRef.current) {
      clearDeferredSpeechSend();
      enqueuePreparedMessage(messageRef.current);
    }
    window.requestAnimationFrame(() => messageInputRef.current?.focus());
  }, [applySpeechTranscript, clearDeferredSpeechSend, enqueuePreparedMessage]);
  const handleSpeechError = useCallback((text: string, fatal: boolean) => {
    if (fatal && speechDraftRef.current) {
      const preserved = cancelEditableSpeechDraft(speechDraftRef.current);
      speechDraftRef.current = null;
      commitMessage(preserved);
    }
    if (fatal) clearDeferredSpeechSend();
    showToast(text, 'error');
  }, [clearDeferredSpeechSend, commitMessage, showToast]);
  const speechInputOptions = useMemo(() => ({
    sessionId: session?.session_id,
    language: speechRecognitionLanguage(language),
    beforeStart: ensureSessionLocale,
    onPartialTranscript: applySpeechTranscript,
    onTranscript: handleSpeechTranscript,
    onError: handleSpeechError,
  }), [applySpeechTranscript, ensureSessionLocale, handleSpeechError, handleSpeechTranscript, language, session?.session_id]);
  const speech = useSpeechToText(speechInputOptions);
  speechInputStateRef.current = {
    requesting: speech.requesting,
    recording: speech.recording,
    transcribing: speech.transcribing,
  };

  const beginSpeechInput = useCallback((provisional = false) => {
    if (speech.requesting || speech.recording || speech.transcribing) return false;
    const input = messageInputRef.current;
    speechDraftRef.current = beginEditableSpeechDraft(
      messageRef.current,
      input?.selectionEnd ?? messageRef.current.length,
    );
    if (!provisional) {
      speechPlayback.stop();
      if (speechOutputEnabled && speechServiceEnabled) {
        void speechPlayback.unlock();
        void speechPlayback.preconnect(session?.session_id || null);
      }
    }
    speechInputStateRef.current = {
      requesting: true,
      recording: false,
      transcribing: false,
    };
    void speech.startRecording(
      provisional && speech.supportsBufferedStart
        ? { bufferedStart: true }
        : undefined,
    );
    return true;
  }, [
    session?.session_id,
    speech.recording,
    speech.requesting,
    speech.startRecording,
    speech.supportsBufferedStart,
    speech.transcribing,
    speechOutputEnabled,
    speechPlayback,
    speechServiceEnabled,
  ]);

  useEffect(() => {
    const playbackError = speechPlayback.error;
    if (!playbackError) {
      lastSpeechPlaybackErrorRef.current = null;
      return;
    }
    if (lastSpeechPlaybackErrorRef.current === playbackError) return;
    lastSpeechPlaybackErrorRef.current = playbackError;
    showToast(playbackError, 'error');
  }, [showToast, speechPlayback.error]);

  useEffect(() => {
    const sessionId = session?.session_id?.trim();
    if (!sessionId || !speechOutputEnabled || !speechServiceEnabled) return;
    void speechPlayback.preconnect(sessionId);
  }, [
    session?.session_id,
    speechOutputEnabled,
    speechPlayback.preconnect,
    speechServiceEnabled,
  ]);

  const stopSpeechInput = useCallback(() => {
    speechInputStateRef.current = {
      requesting: false,
      recording: false,
      transcribing: true,
    };
    speech.activateRecording();
    void speech.stopRecording();
  }, [speech.activateRecording, speech.stopRecording]);

  const cancelSpeechInput = useCallback(() => {
    speech.cancelRecording();
    const activeSpeechDraft = speechDraftRef.current;
    if (activeSpeechDraft) {
      speechDraftRef.current = null;
      commitMessage(cancelEditableSpeechDraft(activeSpeechDraft));
    }
    speechInputStateRef.current = {
      requesting: false,
      recording: false,
      transcribing: false,
    };
  }, [commitMessage, speech.cancelRecording]);

  const activateSpeechHold = useCallback(() => {
    speech.activateRecording();
    speechPlayback.stop();
    if (speechOutputEnabled && speechServiceEnabled) {
      void speechPlayback.unlock();
      void speechPlayback.preconnect(session?.session_id || null);
    }
  }, [session?.session_id, speech.activateRecording, speechOutputEnabled, speechPlayback, speechServiceEnabled]);

  const insertOrdinarySpace = useCallback(() => {
    const input = messageInputRef.current;
    const selection = pendingSpaceSelectionRef.current || {
      start: input?.selectionStart ?? messageRef.current.length,
      end: input?.selectionEnd ?? messageRef.current.length,
    };
    const value = messageRef.current;
    const start = Math.max(0, Math.min(selection.start, value.length));
    const end = Math.max(start, Math.min(selection.end, value.length));
    const nextValue = `${value.slice(0, start)} ${value.slice(end)}`;
    const nextCaret = start + 1;
    pendingSpaceSelectionRef.current = null;
    commitMessage(nextValue);
    window.requestAnimationFrame(() => {
      const currentInput = messageInputRef.current;
      if (!currentInput) return;
      currentInput.focus({ preventScroll: true });
      currentInput.setSelectionRange(nextCaret, nextCaret);
    });
  }, [commitMessage]);

  const clearPushToTalkHoldTimer = useCallback(() => {
    if (pushToTalkHoldTimerRef.current === null) return;
    window.clearTimeout(pushToTalkHoldTimerRef.current);
    pushToTalkHoldTimerRef.current = null;
  }, []);

  startSpeechInputRef.current = () => beginSpeechInput(true);
  stopSpeechInputRef.current = stopSpeechInput;
  cancelSpeechInputRef.current = cancelSpeechInput;
  activateSpeechHoldRef.current = activateSpeechHold;
  insertOrdinarySpaceRef.current = insertOrdinarySpace;

  const applyPushToTalkAction = useCallback((action: PushToTalkShortcutAction) => {
    let nextState = action.state;
    if (action.start) {
      const input = messageInputRef.current;
      pendingSpaceSelectionRef.current = {
        start: input?.selectionStart ?? messageRef.current.length,
        end: input?.selectionEnd ?? messageRef.current.length,
      };
      if (!startSpeechInputRef.current()) {
        pendingSpaceSelectionRef.current = null;
        nextState = {
          ...nextState,
          spaceHeld: false,
          ownsRecording: false,
          activated: false,
        };
      }
    }
    pushToTalkShortcutRef.current = nextState;
    setPushToTalkShortcut(nextState);
    if (action.cancel) cancelSpeechInputRef.current();
    if (action.stop) stopSpeechInputRef.current();
    if (action.insertSpace) insertOrdinarySpaceRef.current();
    if ((action.cancel && !action.insertSpace) || action.stop) {
      pendingSpaceSelectionRef.current = null;
    }
  }, []);

  const canReturnToGuidance = Boolean(
    guidancePath && (guidanceReport || session?.guidance_report_id),
  );
  const speechFinalizingLabel = speech.finalizingProgress?.total
    ? translateTemplate(
      '正在完成最终转写（{completed}/{total}）',
      'Finalizing transcription ({completed}/{total})',
      {
        completed: speech.finalizingProgress.completed,
        total: speech.finalizingProgress.total,
      },
    )
    : translate('正在完成最终转写', 'Finalizing transcription');
  const turns = liveConversation || session?.conversation || [];
  const compactGuidance = useMemo(
    () => buildCompactGuidance(guidanceSections),
    [guidanceSections],
  );
  const compactGuidanceState = compactGuidance.length > 0
    ? 'ready' as const
    : session?.guidance_report_id
      ? guidanceStatus === 'partial_error'
        ? 'error' as const
        : 'loading' as const
      : 'unavailable' as const;
  const rehearsalDimensions = useMemo(
    () => getRehearsalDimensionStatuses(
      session?.rehearsal_context?.covered_dimensions || [],
    ),
    [session?.rehearsal_context?.covered_dimensions],
  );
  const showEmotionReviewPrompt = useMemo(
    () => shouldPromptEmotionReview(session?.conversation || []),
    [session?.conversation],
  );
  const performanceItems = useMemo(
    () => (session?.intent?.performance_items || []).filter(
      (item) => item.goal.trim() || item.current_performance.trim(),
    ),
    [session?.intent?.performance_items],
  );
  const performanceContext = session?.intent?.performance_context || '';
  const {
    employeeName,
    emotionName,
    primaryMotiveName,
    runtimeNotesCount,
    secondaryMotiveNames,
  } = useMemo(() => {
    const motiveById = new Map(options.motives.map((item) => [item.id, item]));
    const primaryMotive = motiveById.get(session?.motivation?.primary_motive_id || '');
    const secondaryNames = (session?.motivation?.secondary_motive_ids || [])
      .map((id) => motiveById.get(id))
      .filter(Boolean)
      .map((item) => item
        ? localizedMotiveName(item.id, item.name, language)
        : '')
      .join(' / ');
    const emotionAnchor = options.emotion_anchors.find(
      (item) => item.id === session?.emotion_state?.current_anchor_id,
    );

    return {
      employeeName: session?.employee_profile?.employee_alias
        || session?.employee_profile?.name
        || translate('员工', 'Employee'),
      emotionName: localizedEmotionName(
        session?.emotion_state?.current_anchor_id,
        emotionAnchor?.name || session?.emotion_state?.current_anchor_id,
        language,
      ) || translate('未设置', 'Not set'),
      primaryMotiveName: localizedMotiveName(
        primaryMotive?.id || session?.motivation?.primary_motive_id,
        primaryMotive?.name || session?.motivation?.primary_motive_id,
        language,
      ) || translate('未设置', 'Not set'),
      runtimeNotesCount: session?.rehearsal_context?.runtime_notes?.length || 0,
      secondaryMotiveNames: secondaryNames || translate('未设置', 'Not set'),
    };
  }, [language, options.emotion_anchors, options.motives, session, translate]);

  useEffect(() => {
    const reportId = session?.guidance_report_id || null;
    if (
      !reportId
      || guidanceReport
      || guidanceStatus !== 'idle'
      || guidanceRestoreAttemptRef.current === reportId
    ) return;
    guidanceRestoreAttemptRef.current = reportId;
    void ensureGuidance().catch((error) => {
      showToast(userFacingErrorMessage(error, undefined, '谈前指导载入失败，请稍后重试。'), 'error');
    });
  }, [ensureGuidance, guidanceReport, guidanceStatus, session?.guidance_report_id, showToast, translate]);

  useEffect(() => {
    if (!speechOptions || speechOptionsHydratedRef.current) return;
    speechOptionsHydratedRef.current = true;
    setSpeechOutputEnabled(speechOptions.enabled);
  }, [speechOptions]);

  useEffect(() => {
    const nextSessionId = session?.session_id || null;
    if (queueSessionIdRef.current !== nextSessionId) {
      messageQueue.clearPending();
      clearPushToTalkHoldTimer();
      applyPushToTalkAction(cancelPushToTalkShortcut(pushToTalkShortcutRef.current));
      clearDeferredSpeechSend();
      queueSessionIdRef.current = nextSessionId;
    }
  }, [applyPushToTalkAction, clearDeferredSpeechSend, clearPushToTalkHoldTimer, messageQueue, session?.session_id]);

  useEffect(() => {
    queueMountedRef.current = true;
    return () => {
      queueMountedRef.current = false;
      messageQueue.clearPending();
    };
  }, [messageQueue]);

  useEffect(() => {
    if (!pushToTalkShortcut.enabled) return undefined;

    const shortcutTargetAllowed = (target: EventTarget | null) => (
      target === messageInputRef.current
      && document.activeElement === messageInputRef.current
    );

    const onKeyDown = (event: KeyboardEvent) => {
      if (
        event.key === 'Escape'
        && (
          pushToTalkShortcutRef.current.ownsRecording
          || shortcutTargetAllowed(event.target)
        )
      ) {
        clearPushToTalkHoldTimer();
        clearDeferredSpeechSend();
        applyPushToTalkAction(cancelPushToTalkShortcut(pushToTalkShortcutRef.current));
        return;
      }
      if (contextOpenRef.current || !shortcutTargetAllowed(event.target)) return;

      const inputState = speechInputStateRef.current;
      const action = pushToTalkShortcutKeyDown(
        pushToTalkShortcutRef.current,
        {
          code: event.code,
          key: event.key,
          repeat: event.repeat,
          isComposing: event.isComposing || event.keyCode === 229,
          altKey: event.altKey,
          ctrlKey: event.ctrlKey,
          metaKey: event.metaKey,
          shiftKey: event.shiftKey,
        },
        !inputState.requesting && !inputState.recording && !inputState.transcribing,
      );
      if (action.preventDefault) event.preventDefault();
      applyPushToTalkAction(action);
      if (action.start) {
        clearPushToTalkHoldTimer();
        pushToTalkHoldTimerRef.current = window.setTimeout(() => {
          pushToTalkHoldTimerRef.current = null;
          const previousState = pushToTalkShortcutRef.current;
          const activation = activatePushToTalkShortcut(previousState);
          if (!previousState.activated && activation.state.activated) {
            activateSpeechHoldRef.current();
          }
          applyPushToTalkAction(activation);
        }, PUSH_TO_TALK_HOLD_MS);
      }
    };

    const onKeyUp = (event: KeyboardEvent) => {
      if (event.code === 'Space' || (!event.code && (event.key === ' ' || event.key === 'Spacebar'))) {
        clearPushToTalkHoldTimer();
      }
      const action = pushToTalkShortcutKeyUp(
        pushToTalkShortcutRef.current,
        { code: event.code, key: event.key },
      );
      if (action.preventDefault) event.preventDefault();
      applyPushToTalkAction(action);
    };

    const releaseShortcut = () => {
      clearPushToTalkHoldTimer();
      applyPushToTalkAction(cancelPushToTalkShortcut(pushToTalkShortcutRef.current));
    };
    const onVisibilityChange = () => {
      if (document.visibilityState === 'hidden') releaseShortcut();
    };

    window.addEventListener('keydown', onKeyDown, true);
    window.addEventListener('keyup', onKeyUp, true);
    window.addEventListener('blur', releaseShortcut);
    document.addEventListener('visibilitychange', onVisibilityChange);
    return () => {
      window.removeEventListener('keydown', onKeyDown, true);
      window.removeEventListener('keyup', onKeyUp, true);
      window.removeEventListener('blur', releaseShortcut);
      document.removeEventListener('visibilitychange', onVisibilityChange);
      clearPushToTalkHoldTimer();
      const action = cancelPushToTalkShortcut(pushToTalkShortcutRef.current);
      pushToTalkShortcutRef.current = action.state;
      if (action.cancel) cancelSpeechInputRef.current();
      if (action.stop) stopSpeechInputRef.current();
    };
  }, [applyPushToTalkAction, clearDeferredSpeechSend, clearPushToTalkHoldTimer, pushToTalkShortcut.enabled]);

  useEffect(() => {
    if (
      !sendAfterSpeechFinalizes
      || speech.requesting
      || speech.recording
      || speech.transcribing
    ) return;
    clearDeferredSpeechSend();
    enqueuePreparedMessage(messageRef.current);
  }, [
    clearDeferredSpeechSend,
    enqueuePreparedMessage,
    sendAfterSpeechFinalizes,
    speech.recording,
    speech.requesting,
    speech.transcribing,
  ]);

  const handleChatScroll = useCallback((event: ReactUIEvent<HTMLDivElement>) => {
    const node = event.currentTarget;
    shouldFollowChatTailRef.current = (
      node.scrollHeight - node.scrollTop - node.clientHeight <= 64
    );
  }, []);

  useEffect(() => {
    if (!shouldFollowChatTailRef.current || chatScrollTimerRef.current !== null) return;
    const elapsed = performance.now() - lastChatScrollAtRef.current;
    const delay = rehearsalStreaming ? Math.max(0, 66 - elapsed) : 0;
    chatScrollTimerRef.current = window.setTimeout(() => {
      chatScrollTimerRef.current = null;
      const node = chatListRef.current;
      if (!node || !shouldFollowChatTailRef.current) return;
      lastChatScrollAtRef.current = performance.now();
      node.scrollTo({
        top: node.scrollHeight,
        behavior: rehearsalStreaming ? 'auto' : 'smooth',
      });
    }, delay);
  }, [liveConversation, rehearsalStreaming, turns.length]);

  useEffect(() => () => {
    if (chatScrollTimerRef.current !== null) {
      window.clearTimeout(chatScrollTimerRef.current);
      chatScrollTimerRef.current = null;
    }
  }, []);

  useEffect(() => {
    const invalidateInputMeasurement = () => {
      messageInputMaxHeightRef.current = null;
    };
    window.addEventListener('resize', invalidateInputMeasurement, { passive: true });
    return () => window.removeEventListener('resize', invalidateInputMeasurement);
  }, []);

  useEffect(() => {
    const frameId = window.requestAnimationFrame(() => {
      const textarea = messageInputRef.current;
      if (!textarea) return;
      textarea.style.height = 'auto';
      let maxHeight = messageInputMaxHeightRef.current;
      if (maxHeight === null) {
        const styles = window.getComputedStyle(textarea);
        const lineHeight = Number.parseFloat(styles.lineHeight) || 22;
        const paddingY = Number.parseFloat(styles.paddingTop) + Number.parseFloat(styles.paddingBottom);
        const borderY = Number.parseFloat(styles.borderTopWidth) + Number.parseFloat(styles.borderBottomWidth);
        maxHeight = Math.ceil(lineHeight * 5 + paddingY + borderY);
        messageInputMaxHeightRef.current = maxHeight;
      }
      const scrollHeight = textarea.scrollHeight;
      const nextHeight = Math.min(scrollHeight, maxHeight);
      textarea.style.height = `${nextHeight}px`;
      textarea.style.overflowY = scrollHeight > maxHeight ? 'auto' : 'hidden';
      textarea.scrollTop = scrollHeight;
    });
    return () => window.cancelAnimationFrame(frameId);
  }, [message]);

  const submit = async () => {
    const text = messageRef.current.trim();
    const inputState = speechInputStateRef.current;
    if (inputState.requesting || inputState.transcribing) return;
    if (inputState.recording) {
      const correctedTranscript = await speech.stopRecording();
      if (speechDraftRef.current && correctedTranscript.trim()) {
        applySpeechTranscript(correctedTranscript);
      }
      const immediateText = messageRef.current.trim();
      speechDraftRef.current = null;
      if (immediateText) enqueuePreparedMessage(immediateText);
      return;
    }
    if (!text) return;
    enqueuePreparedMessage(text);
  };

  const requestMessageSubmit = () => {
    if (speechOutputEnabled && speechServiceEnabled) {
      void speechPlayback.unlock();
    }
    clearPushToTalkHoldTimer();
    const shortcutOwnsRecording = pushToTalkShortcutRef.current.ownsRecording;
    if (shortcutOwnsRecording) {
      const shortcutAction = pushToTalkShortcutRef.current.activated
        ? pushToTalkShortcutKeyUp(
            pushToTalkShortcutRef.current,
            { code: 'Space', key: ' ' },
          )
        : cancelPushToTalkShortcut(pushToTalkShortcutRef.current);
      applyPushToTalkAction(shortcutAction);
    }
    const inputState = speechInputStateRef.current;
    if (
      pushToTalkShortcutRef.current.enabled
      && (inputState.requesting || inputState.transcribing)
    ) {
      sendAfterSpeechFinalizesRef.current = true;
      setSendAfterSpeechFinalizes(true);
      return;
    }
    void submit();
  };

  const applyContext = async () => {
    const notes = normalizeRuntimeNoteDrafts(runtimeNoteDrafts);
    const savedNotes = normalizeRuntimeNoteDrafts(
      session?.rehearsal_context?.runtime_notes || [],
    );
    if (!notes.length && !savedNotes.length) {
      showToast('请先输入新增信息或模拟要求。', 'error');
      return;
    }
    const updated = await updateRehearsalContext({
      clear_context: true,
      runtime_notes: notes,
    });
    if (updated) {
      const persistedNotes = runtimeNotesToDrafts(
        updated.rehearsal_context?.runtime_notes,
      );
      setRuntimeNoteDrafts(persistedNotes);
      setContextOpen(false);
    }
  };

  const retryGuidance = useCallback(() => {
    void ensureGuidance().catch((error) => {
      showToast(userFacingErrorMessage(error, undefined, '谈前指导载入失败，请稍后重试。'), 'error');
    });
  }, [ensureGuidance, showToast, translate]);
  const finish = async () => {
    clearPushToTalkHoldTimer();
    applyPushToTalkAction(cancelPushToTalkShortcut(pushToTalkShortcutRef.current));
    clearDeferredSpeechSend();
    await endRehearsal();
    navigate(reportPath);
  };
  const retryQueuedMessages = useCallback(() => {
    if (!messageQueue.retry()) {
      showToast('当前没有需要重试的追加消息。', 'error');
    }
  }, [messageQueue, showToast, translate]);
  const openContext = useCallback(() => {
    const savedNotes = runtimeNotesToDrafts(
      session?.rehearsal_context?.runtime_notes,
    );
    setRuntimeNoteDrafts(savedNotes);
    setContextOpen(true);
  }, [session?.rehearsal_context?.runtime_notes]);

  const updateRuntimeNoteDraft = useCallback((index: number, value: string) => {
    setRuntimeNoteDrafts((current) => current.map(
      (item, itemIndex) => itemIndex === index ? value : item,
    ));
  }, []);

  const addRuntimeNoteDraft = useCallback(() => {
    setRuntimeNoteDrafts((current) => [...current, '']);
  }, []);

  const removeRuntimeNoteDraft = useCallback((index: number) => {
    setRuntimeNoteDrafts((current) => {
      const next = current.filter((_, itemIndex) => itemIndex !== index);
      return next.length ? next : [''];
    });
  }, []);
  const returnToGuidance = useCallback(
    () => navigate(guidancePath || '/app/guidance'),
    [guidancePath, navigate],
  );
  const pushToTalkArming = pushToTalkShortcut.enabled
    && pushToTalkShortcut.spaceHeld
    && pushToTalkShortcut.ownsRecording
    && !pushToTalkShortcut.activated;
  const pushToTalkListening = pushToTalkShortcut.ownsRecording
    && pushToTalkShortcut.activated
    && (speech.requesting || speech.recording || pushToTalkShortcut.spaceHeld);
  const speechRecordingVisible = speech.recording && !pushToTalkArming;
  const speechRequestingVisible = speech.requesting && !pushToTalkArming;
  const idleSmartSpaceStatus = rehearsalStreaming
    ? translate(
      '员工回复中 · 仍可长按空格录入，Enter 后自动追加',
      'Employee responding · Hold Space to speak · Enter queues the message',
    )
    : translate(
      '长按空格说话 · 松开结束 · Enter 发送',
      'Hold Space to speak · Release to stop · Enter to send',
    );
  const smartSpaceStatus = sendAfterSpeechFinalizes
    ? translate('正在转写 · 完成后自动发送', 'Transcribing · Will send when complete')
    : pushToTalkArming
      ? idleSmartSpaceStatus
      : speech.transcribing
        ? translate('正在转写 · 按 Enter 可在完成后发送', 'Transcribing · Press Enter to send when complete')
        : pushToTalkListening
          ? speech.requesting
            ? translate('正在开启麦克风 · 松开空格取消', 'Opening microphone · Release Space to cancel')
            : translate('正在聆听 · 松开空格结束', 'Listening · Release Space to stop')
          : speech.requesting
            ? translate('正在开启麦克风 · 点击麦克风取消', 'Opening microphone · Click the microphone to cancel')
            : speech.recording
              ? translate('正在聆听 · 点击麦克风结束', 'Listening · Click the microphone to stop')
              : idleSmartSpaceStatus;

  return (
    <section id="screen-rehearsal" className="screen active">
      <div className="page-intro compact-intro">
        <h1>{translate('对话预演', 'Conversation rehearsal')}</h1>
      </div>
      <div className="rehearsal-workbench">
        <section className="soft-card rehearsal-card">
          <div className="rehearsal-head">
            {speechServiceEnabled && (
              <label className="speech-output-toggle" title={translate('开启或关闭员工回复语音', 'Enable or disable spoken employee responses')}>
                <input
                  type="checkbox"
                  checked={speechOutputEnabled}
                  onChange={(event) => {
                    const enabled = event.target.checked;
                    setSpeechOutputEnabled(enabled);
                    if (!enabled) {
                      speechPlayback.stop();
                      return;
                    }
                    void speechPlayback.unlock();
                    void speechPlayback.preconnect(session?.session_id || null);
                  }}
                />
                <span className="speech-toggle-track" aria-hidden="true">
                  <span />
                </span>
                <span>{translate('员工语音', 'Employee voice')}</span>
              </label>
            )}
            <button className="btn btn-danger-ghost rehearsal-end-button" type="button" onClick={finish}>
              <CircleStop size={17} aria-hidden="true" />
              <span>{translate('结束预演', 'End rehearsal')}</span>
            </button>
          </div>
          <ChatList
            turns={turns}
            employeeName={employeeName}
            streaming={Boolean(liveConversation && rehearsalStreaming)}
            listRef={chatListRef}
            onScroll={handleChatScroll}
          />
          <div className="message-bar">
            <div
              className={`rehearsal-smart-space-status${pushToTalkArming ? ' is-arming' : ''}${pushToTalkListening ? ' is-listening' : ''}${speech.transcribing ? ' is-transcribing' : ''}`}
              role="status"
              aria-live={pushToTalkArming ? 'off' : 'polite'}
              aria-atomic="true"
            >
              <span className="rehearsal-smart-space-dot" aria-hidden="true" />
              <span id="rehearsal-smart-space-status-text">{smartSpaceStatus}</span>
            </div>
            <span id="rehearsal-smart-space-help" className="sr-only">
              {translate(
                '智能空格始终开启。长按空格说话，松开结束录音，按回车发送。',
                'Smart Space is always enabled. Hold Space to speak, release to stop recording, and press Enter to send.',
              )}
            </span>
            {(messageQueueSnapshot.waiting > 0 || messageQueueSnapshot.paused) && (
              <div className={`rehearsal-message-queue-status${messageQueueSnapshot.paused ? ' is-paused' : ''}`} role="status" aria-live="polite">
                <span>
                  {messageQueueSnapshot.paused
                    ? translateTemplate(
                      '等待重试的追加消息：{count} 条',
                      'Queued messages waiting to retry: {count}',
                      { count: messageQueueSnapshot.size },
                    )
                    : translateTemplate(
                      '已追加 {count} 条，将在当前回复后依次发送',
                      'Queued messages to send after the current response: {count}',
                      { count: messageQueueSnapshot.waiting },
                    )}
                </span>
                {messageQueueSnapshot.paused && (
                  <button type="button" onClick={retryQueuedMessages}>{translate('重试', 'Retry')}</button>
                )}
              </div>
            )}
            <textarea
              ref={messageInputRef}
              value={message}
              aria-describedby="rehearsal-smart-space-help"
              onChange={(event) => {
                const nextValue = event.currentTarget.value;
                const activeSpeechDraft = speechDraftRef.current;
                if (activeSpeechDraft) {
                  speechDraftRef.current = editEditableSpeechDraft(
                    activeSpeechDraft,
                    nextValue,
                    event.currentTarget.selectionStart,
                  );
                }
                commitMessage(nextValue);
              }}
              onSelect={(event) => {
                const activeSpeechDraft = speechDraftRef.current;
                if (!activeSpeechDraft) return;
                speechDraftRef.current = moveEditableSpeechCaret(
                  activeSpeechDraft,
                  event.currentTarget.selectionStart,
                );
              }}
              onKeyDown={(event) => {
                if (
                  event.key === 'Enter'
                  && !event.shiftKey
                  && !event.repeat
                  && !event.nativeEvent.isComposing
                  && event.nativeEvent.keyCode !== 229
                ) {
                  event.preventDefault();
                  requestMessageSubmit();
                }
              }}
              placeholder={translate('请输入你的回复...', 'Enter your response...')}
              rows={1}
            />
            <button
              className={`mic-button${speechRecordingVisible ? ` recording realtime-${speech.realtimeStatus}` : ''}${speechRequestingVisible || speech.transcribing ? ' transcribing' : ''}`}
              title={pushToTalkArming
                ? translate('继续按住空格以开始语音，立即松开会输入空格', 'Keep holding Space to start speech input; release now to type a space')
                : speech.recording
                ? speech.realtimeStatus === 'connected'
                  ? translate('停止录音（正在实时转写）', 'Stop recording (live transcription active)')
                  : speech.realtimeStatus === 'connecting'
                    ? translate('停止录音（正在连接本地语音模型）', 'Stop recording (connecting to the local speech model)')
                    : translate('停止录音（停止后完成转写）', 'Stop recording (transcription completes after stopping)')
                : speech.requesting
                  ? translate('正在请求麦克风权限', 'Requesting microphone permission')
                  : speech.transcribing
                    ? speechFinalizingLabel
                    : speech.readinessStatus === 'warming'
                      ? translate('实时语音准备中', 'Preparing live speech input')
                      : speech.readinessStatus === 'degraded'
                        ? translate('语音输入（实时预览暂不可用）', 'Speech input (live preview unavailable)')
                        : translate('语音输入', 'Speech input')}
              aria-label={pushToTalkArming
                ? translate('正在判断长按空格', 'Detecting Space hold')
                : speech.recording
                  ? translate('停止录音', 'Stop recording')
                  : speech.requesting
                    ? translate('正在请求麦克风权限', 'Requesting microphone permission')
                    : speech.transcribing
                      ? translate('正在转写', 'Transcribing')
                      : speech.readinessStatus === 'warming'
                        ? translate('实时语音准备中', 'Preparing live speech input')
                        : translate('开始语音输入', 'Start speech input')}
              aria-pressed={speechRecordingVisible}
              type="button"
              disabled={speech.transcribing}
              onClick={() => {
                clearPushToTalkHoldTimer();
                if (pushToTalkShortcutRef.current.ownsRecording) {
                  const shortcutAction = pushToTalkShortcutRef.current.activated
                    ? pushToTalkShortcutKeyUp(
                        pushToTalkShortcutRef.current,
                        { code: 'Space', key: ' ' },
                      )
                    : cancelPushToTalkShortcut(pushToTalkShortcutRef.current);
                  applyPushToTalkAction(shortcutAction);
                  return;
                }
                if (speech.recording || speech.requesting) {
                  stopSpeechInput();
                  return;
                }
                beginSpeechInput();
              }}
            >
              {speechRecordingVisible && (
                <span className="voice-wave-glyph" aria-hidden="true">
                  <i />
                  <i />
                  <i />
                  <i />
                  <i />
                </span>
              )}
              {(speechRequestingVisible || (!speechRecordingVisible && !speech.transcribing && speech.readinessStatus === 'warming' && !pushToTalkArming)) && (
                <LoaderCircle className="spin" size={20} aria-hidden="true" />
              )}
              {!speechRequestingVisible && !speechRecordingVisible && (pushToTalkArming || speech.transcribing || speech.readinessStatus !== 'warming') && (
                <Mic size={20} aria-hidden="true" />
              )}
            </button>
            <button
              className={`btn btn-primary send-button${rehearsalStreaming ? ' is-streaming' : ''}`}
              type="button"
              title={rehearsalStreaming
                ? translate('追加到下一轮', 'Queue for the next turn')
                : speech.recording
                  ? translate('停止录音并发送', 'Stop recording and send')
                  : translate('发送', 'Send')}
              onClick={requestMessageSubmit}
              disabled={speech.requesting || speech.transcribing || !message.trim()}
            >
              <ArrowUp size={19} strokeWidth={2.2} aria-hidden="true" />
              <span className="sr-only">
                {rehearsalStreaming
                  ? translate('追加到下一轮', 'Queue for the next turn')
                  : speech.recording
                    ? translate('停止录音并发送', 'Stop recording and send')
                    : translate('发送', 'Send')}
              </span>
            </button>
          </div>
        </section>

        <ConversationContextPanel
          employeeProfile={session?.employee_profile || null}
          employeeName={employeeName}
          primaryMotiveName={primaryMotiveName}
          secondaryMotiveNames={secondaryMotiveNames}
          emotionName={emotionName}
          runtimeNotesCount={runtimeNotesCount}
          onEdit={openContext}
          onReturnToGuidance={canReturnToGuidance ? returnToGuidance : undefined}
          dimensions={rehearsalDimensions}
          showEmotionReviewPrompt={showEmotionReviewPrompt}
          performanceItems={performanceItems}
          performanceContext={performanceContext}
          compactGuidance={compactGuidance}
          compactGuidanceState={compactGuidanceState}
          onRetryGuidance={compactGuidanceState === 'error' ? retryGuidance : undefined}
        />
      </div>

      <div className={`rehearsal-context-widget ${contextOpen ? 'open' : ''}`}>
        {contextOpen && (
          <div
            className="rehearsal-context-panel"
            role="dialog"
            aria-labelledby="rehearsal-context-editor-title"
            onKeyDown={(event) => {
              if (event.key !== 'Escape') return;
              event.stopPropagation();
              setContextOpen(false);
            }}
          >
            <div className="context-panel-head">
              <strong id="rehearsal-context-editor-title">{translate('修改会话背景', 'Edit session context')}</strong>
              <button className="context-close" type="button" aria-label={translate('关闭', 'Close')} onClick={() => setContextOpen(false)}>
                <X size={17} aria-hidden="true" />
              </button>
            </div>
            <div className="context-note-list">
              {runtimeNoteDrafts.map((draft, index) => (
                <div className="context-note-item" key={index}>
                  <label className="context-note-field">
                    <span>{translateTemplate('背景信息 {index}', 'Context item {index}', { index: index + 1 })}</span>
                    <textarea
                      autoFocus={index === 0}
                      aria-label={translateTemplate('会话背景 {index}', 'Session context {index}', { index: index + 1 })}
                      value={draft}
                      onChange={(event) => updateRuntimeNoteDraft(index, event.target.value)}
                      disabled={rehearsalStreaming}
                      placeholder={translate(
                        '补充员工背景、临时事件或本轮模拟要求',
                        'Add employee context, a recent event, or instructions for this rehearsal',
                      )}
                      rows={3}
                    />
                  </label>
                  <button
                    className="context-note-remove"
                    type="button"
                    aria-label={translateTemplate('删除背景信息 {index}', 'Delete context item {index}', { index: index + 1 })}
                    title={translate('删除这条背景信息', 'Delete this context item')}
                    onClick={() => removeRuntimeNoteDraft(index)}
                    disabled={rehearsalStreaming}
                  >
                    <Trash2 size={16} aria-hidden="true" />
                  </button>
                </div>
              ))}
            </div>
            <div className="context-actions">
              <button
                className="context-note-add"
                type="button"
                onClick={addRuntimeNoteDraft}
                disabled={rehearsalStreaming}
              >
                <Plus size={16} aria-hidden="true" />
                <span>{translate('新增一条', 'Add another')}</span>
              </button>
              <button className="btn btn-primary context-apply-button" type="button" onClick={applyContext} disabled={rehearsalStreaming}>
                <Check size={17} aria-hidden="true" />
                <span>{translate('保存修改', 'Save changes')}</span>
              </button>
            </div>
          </div>
        )}
      </div>
    </section>
  );
}

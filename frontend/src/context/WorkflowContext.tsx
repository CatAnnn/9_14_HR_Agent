import { createContext, PropsWithChildren, useCallback, useContext, useEffect, useMemo, useRef, useState, type SetStateAction } from 'react';
import { api } from '../api/client';
import type {
  BigFivePersonality,
  CoachReport,
  CoachTaskDraft,
  CoachTaskResult,
  ConversationTurn,
  EmployeeProfile,
  EmployeeRecord,
  GuidancePointGroup,
  GuidanceReport,
  GuidanceSectionDraft,
  GuidanceSectionKey,
  IntentGoalPerformanceItem,
  IntentPerformanceDraftResponse,
  RehearsalClientTiming,
  RehearsalContextUpdatePayload,
  RehearsalSpeechRequest,
  RehearsalTurnTiming,
  SessionLocale,
  WorkflowStreamStatus,
  SessionState,
  SetupOptions,
} from '../types/domain';
import {
  composeProfileText,
  DEFAULT_PROFILE_TEXT,
  employeeRecordText,
  profileFromEmployeeRecord,
} from '../utils/format';
import {
  buildEmployeeSearchParams,
  EMPLOYEE_DIRECTORY_REFRESH_MS,
  filterEmployeeRecords,
  shouldAwaitFreshEmployeeDirectory,
} from '../utils/employeeSearch';
import { normalizeLatestEmployeeSetupSettings } from '../utils/employeeSetupSettings';
import { isCompleteCoachReport } from '../utils/coachReportStatus';
import { userFacingErrorMessage } from '../utils/displayText';
import { useLanguage } from '../i18n/LanguageContext';
import {
  SessionLocaleCoordinator,
  SessionLocaleSyncError,
  type SessionLocaleOperationToken,
} from '../utils/sessionLocaleCoordinator';
import {
  clearWorkflowDrafts,
  moveWorkflowDraft,
  readWorkflowDraft,
  removeWorkflowDraft,
  shouldInvalidateStoredSession,
  writeWorkflowDraft,
  type WorkflowDraft,
  type WorkflowDraftFileMetadata,
  type WorkflowDraftStorage,
} from '../utils/workflowDraftStorage';
import {
  recoverWorkflowSession,
  WorkflowSessionCreationDisabledError,
} from '../utils/workflowSessionRecovery';
import {
  createRehearsalRequestId,
  hasCompletedRehearsalRequest,
} from '../utils/rehearsalRequest';
import { createRehearsalStreamDraft } from '../utils/rehearsalStreamDraft';

interface ToastState {
  message: string;
  type: 'ok' | 'error';
}

interface EmployeeLookupOptions {
  autoSelectSingle?: boolean;
  silent?: boolean;
  signal?: AbortSignal;
}

interface WorkflowFeedbackContextValue {
  loading: { active: boolean; text: string };
  toast: ToastState | null;
}

interface WorkflowNavigationContextValue {
  session: SessionState | null;
  hasIntentOptions: boolean;
  selectedIntentId: string | null;
  guidanceStatus: WorkflowStreamStatus;
  reportStatus: WorkflowStreamStatus;
  bootstrapStatus: 'idle' | 'loading' | 'ready' | 'error';
  bootstrap: () => Promise<void>;
  ensureGuidance: () => Promise<void>;
  ensureReport: (force?: boolean) => Promise<void>;
}

interface SetupSnapshot {
  profile?: EmployeeProfile | null;
  intentId?: string | null;
  intentPerformanceContext?: string | null;
  intentPerformanceItems?: IntentGoalPerformanceItem[] | null;
  personality?: BigFivePersonality | null;
  primaryMotiveId?: string | null;
  secondaryMotiveIds?: string[] | null;
}

interface MotiveSelectionState {
  hydrated: boolean;
  primaryMotiveId: string | null;
  secondaryMotiveIds: string[];
}

interface LocaleBoundPayload {
  locale?: SessionLocale | null;
}

interface LocaleOperation {
  session: SessionState;
  token: SessionLocaleOperationToken;
}

class StaleLocaleResultError extends Error {
  constructor() {
    super('语言已切换，已忽略切换前生成的结果，请重试当前操作。');
    this.name = 'StaleLocaleResultError';
  }
}

interface WorkflowContextValue {
  sessionId: string | null;
  session: SessionState | null;
  options: SetupOptions;
  profileText: string;
  selectedFile: File | null;
  selectedFileMetadata: WorkflowDraftFileMetadata | null;
  employeeResults: EmployeeRecord[];
  selectedEmployee: EmployeeRecord | null;
  selectedIntentId: string | null;
  intentPerformanceDrafts: Record<string, IntentPerformanceDraftResponse>;
  selectedPersonality: BigFivePersonality;
  selectedPrimaryMotiveId: string | null;
  selectedSecondaryMotiveIds: string[];
  guidanceReport: GuidanceReport | null;
  guidanceSections: GuidanceSectionDraft[];
  guidanceStatus: WorkflowStreamStatus;
  coachReport: CoachReport | null;
  reportStatus: WorkflowStreamStatus;
  coachTasks: CoachTaskDraft[];
  liveConversation: ConversationTurn[] | null;
  rehearsalStreaming: boolean;
  rehearsalDraft: string;
  rehearsalRuntimeNote: string;
  displayedProfile: EmployeeProfile | null;
  setProfileText: (text: string) => void;
  setSelectedFile: (file: File | null) => void;
  discardSelectedFileMetadata: () => void;
  setSelectedIntentId: (id: string) => void;
  setIntentPerformanceDrafts: (
    update: SetStateAction<Record<string, IntentPerformanceDraftResponse>>,
  ) => void;
  updatePersonalityDimension: (dimension: keyof BigFivePersonality, value: number) => void;
  setSelectedPrimaryMotiveId: (id: string) => void;
  setSelectedSecondaryMotiveIds: (update: SetStateAction<string[]>) => void;
  setRehearsalDraft: (update: SetStateAction<string>) => void;
  setRehearsalRuntimeNote: (update: SetStateAction<string>) => void;
  bootstrap: () => Promise<void>;
  lookupEmployee: (query: string, options?: EmployeeLookupOptions) => Promise<void>;
  selectEmployee: (record: EmployeeRecord) => void;
  confirmProfile: () => Promise<boolean>;
  generateIntentPerformanceDraft: (
    intentId: string,
  ) => Promise<IntentPerformanceDraftResponse>;
  streamIntentPerformanceDraft: (
    intentId: string,
    onEvent: (event: string, data: Record<string, unknown>) => void,
    signal?: AbortSignal,
  ) => Promise<void>;
  confirmIntent: (
    performanceContext: string,
    performanceItems: IntentGoalPerformanceItem[],
  ) => Promise<void>;
  confirmSimulation: () => Promise<void>;
  ensureGuidance: () => Promise<void>;
  startNewSession: () => Promise<SessionState>;
  ensureSessionLocale: () => Promise<void>;
  updateRehearsalContext: (payload: RehearsalContextUpdatePayload) => Promise<SessionState | null>;
  sendMessage: (
    message: string,
    speech?: RehearsalSpeechRequest,
    onSpeechFailure?: (event: string, data: Record<string, unknown>) => void,
    requestId?: string,
  ) => Promise<boolean>;
  endRehearsal: () => Promise<void>;
  ensureReport: (force?: boolean) => Promise<void>;
  exportReport: () => void;
  showToast: (message: string, type?: 'ok' | 'error') => void;
}

const DEFAULT_BIG_FIVE: BigFivePersonality = {
  openness: 50,
  conscientiousness: 50,
  extraversion: 50,
  agreeableness: 50,
  neuroticism: 50,
};
const emptyOptions: SetupOptions = { intents: [], motives: [], emotion_anchors: [], default_big_five: DEFAULT_BIG_FIVE };
const guidanceSectionSeeds: Array<{ key: GuidanceSectionKey; title: string }> = [
  { key: 'opening_suggestion', title: '开场定调与绩效结果对齐' },
  { key: 'risk_preview', title: '情绪承接、接纳与共情' },
  { key: 'response_strategies', title: '产出与标准' },
  { key: 'safer_phrases', title: '总结与差异化发展计划' },
];
const coachTaskSeeds: Array<{ task_id: string; task_name: string }> = [
  { task_id: 'opening_evaluation', task_name: '开场定调与绩效结果对齐评估' },
  { task_id: 'emotion_evaluation', task_name: '情绪承接评估' },
  { task_id: 'output_expectations_evaluation', task_name: '从情绪回归产出与标准评估' },
  { task_id: 'development_plan_evaluation', task_name: '总结与差异化发展计划评估' },
];
const WORKFLOW_DRAFT_PERSIST_DELAY_MS = 250;
const REHEARSAL_STREAM_RENDER_INTERVAL_MS = 50;
const WorkflowContext = createContext<WorkflowContextValue | null>(null);
const WorkflowFeedbackContext = createContext<WorkflowFeedbackContextValue | null>(null);
const WorkflowNavigationContext = createContext<WorkflowNavigationContextValue | null>(null);

function createGuidanceSections(status: GuidanceSectionDraft['status'] = 'idle'): GuidanceSectionDraft[] {
  return guidanceSectionSeeds.map((section) => ({
    ...section,
    text: '',
    items: null,
    point_groups: null,
    status,
    error: null,
  }));
}

function isGuidancePointGroups(value: unknown): value is GuidancePointGroup[] {
  return Array.isArray(value)
    && value.length > 0
    && value.every((group) => (
      typeof group === 'object'
      && group !== null
      && typeof (group as GuidancePointGroup).title === 'string'
      && (group as GuidancePointGroup).title.trim().length > 0
      && ((group as GuidancePointGroup).summary == null || (
        typeof (group as GuidancePointGroup).summary === 'string'
        && (group as GuidancePointGroup).summary!.trim().length > 0
      ))
      && Array.isArray((group as GuidancePointGroup).details)
      && (group as GuidancePointGroup).details.length > 0
      && (group as GuidancePointGroup).details.every(
        (detail) => typeof detail === 'string' && detail.trim().length > 0,
      )
    ));
}

function guidanceSectionsFromReport(report: GuidanceReport): GuidanceSectionDraft[] {
  return guidanceSectionSeeds.map((section) => {
    const groupedBySection: Partial<Record<GuidanceSectionKey, unknown>> = {
      opening_suggestion: report.dimension_points?.start,
      risk_preview: report.dimension_points?.emotion,
      response_strategies: report.dimension_points?.requirement,
      safer_phrases: report.dimension_points?.plan,
    };
    const pointGroups = groupedBySection[section.key];
    if (isGuidancePointGroups(pointGroups)) {
      return {
        ...section,
        text: '',
        items: null,
        point_groups: pointGroups,
        status: 'done' as const,
        error: null,
      };
    }

    const legacyItems = section.key === 'opening_suggestion'
      ? [report.purpose, report.opening_suggestion]
      : report[section.key];
    if (
      !Array.isArray(legacyItems)
      || legacyItems.some((item) => typeof item !== 'string' || !item.trim())
    ) {
      throw new Error(`谈前指导最终报告字段 ${section.key} 不完整。`);
    }
    return {
      ...section,
      text: '',
      items: legacyItems as string[],
      point_groups: null,
      status: 'done' as const,
      error: null,
    };
  });
}

function createCoachTasks(status: CoachTaskDraft['status'] = 'idle'): CoachTaskDraft[] {
  return coachTaskSeeds.map((task) => ({ ...task, status, result: null, summary: '', score: null, error: null }));
}

function asGuidanceSectionKey(value: unknown): GuidanceSectionKey | null {
  return typeof value === 'string' && guidanceSectionSeeds.some((section) => section.key === value) ? value as GuidanceSectionKey : null;
}

function clampPersonalityScore(value: unknown): number {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return 50;
  return Math.max(0, Math.min(100, Math.round(numeric)));
}

function normalizeBigFive(value?: Partial<BigFivePersonality> | null): BigFivePersonality {
  return {
    openness: clampPersonalityScore(value?.openness ?? DEFAULT_BIG_FIVE.openness),
    conscientiousness: clampPersonalityScore(value?.conscientiousness ?? DEFAULT_BIG_FIVE.conscientiousness),
    extraversion: clampPersonalityScore(value?.extraversion ?? DEFAULT_BIG_FIVE.extraversion),
    agreeableness: clampPersonalityScore(value?.agreeableness ?? DEFAULT_BIG_FIVE.agreeableness),
    neuroticism: clampPersonalityScore(value?.neuroticism ?? DEFAULT_BIG_FIVE.neuroticism),
  };
}

function samePersonality(left: BigFivePersonality | null | undefined, right: BigFivePersonality | null | undefined): boolean {
  return JSON.stringify(normalizeBigFive(left)) === JSON.stringify(normalizeBigFive(right));
}

function sessionIntentId(state: SessionState | null): string | null {
  return state?.intent?.intent_id || state?.intent?.id || null;
}

function sessionIntentPerformanceContext(state: SessionState | null): string | null {
  return typeof state?.intent?.performance_context === 'string'
    ? state.intent.performance_context
    : null;
}

function sessionPrimaryMotiveId(state: SessionState | null): string | null {
  return state?.motivation?.primary_motive_id || null;
}

function normalizeSecondaryMotiveIds(
  ids: string[] | null | undefined,
  primaryMotiveId: string | null | undefined,
): string[] {
  return (ids || [])
    .filter(Boolean)
    .filter((id, index, all) => id !== primaryMotiveId && all.indexOf(id) === index)
    .slice(0, 2);
}

function sessionSecondaryMotiveIds(state: SessionState | null): string[] {
  return normalizeSecondaryMotiveIds(state?.motivation?.secondary_motive_ids, sessionPrimaryMotiveId(state));
}

function validSecondaryMotiveCount(ids: string[] | null | undefined): ids is string[] {
  return Array.isArray(ids) && ids.length <= 2;
}

function hasDownstreamProgress(state: SessionState | null): boolean {
  return Boolean(
    state?.setup_ready ||
    state?.guidance_report_id ||
    state?.coach_report_id ||
    state?.conversation?.length,
  );
}

function profileFingerprint(profile: EmployeeProfile | null | undefined): string {
  if (!profile) return '';
  const stable: Record<string, unknown> = {};
  Object.keys(profile)
    .filter((key) => key !== 'source_profile_text' && key !== 'supplemental_info')
    .sort()
    .forEach((key) => {
      const value = profile[key];
      if (value !== undefined && value !== null && value !== '') stable[key] = value;
    });
  return JSON.stringify(stable);
}

function sameProfile(left: EmployeeProfile | null | undefined, right: EmployeeProfile | null | undefined): boolean {
  if (!left && !right) return true;
  if (!left || !right) return false;
  if (left.employee_id && right.employee_id) return left.employee_id === right.employee_id;
  return profileFingerprint(left) === profileFingerprint(right);
}

function monotonicNowMs(): number {
  return typeof performance !== 'undefined' ? performance.now() : Date.now();
}

function elapsedClientMs(startedAt: number, finishedAt: number): number {
  return Math.max(0, Math.round(finishedAt - startedAt));
}

function asRehearsalTurnTiming(value: unknown): RehearsalTurnTiming | undefined {
  if (!value || typeof value !== 'object') return undefined;
  const candidate = value as Partial<RehearsalTurnTiming>;
  if (
    typeof candidate.attempt_id !== 'string'
    || typeof candidate.schema_version !== 'number'
    || !candidate.summary_ms
    || !Array.isArray(candidate.stages)
  ) return undefined;
  return candidate as RehearsalTurnTiming;
}

function attachLatestEmployeeTiming(
  state: SessionState,
  clientTiming: RehearsalClientTiming,
  serverTiming?: RehearsalTurnTiming,
): SessionState {
  const conversation = [...(state.conversation || [])];
  for (let index = conversation.length - 1; index >= 0; index -= 1) {
    const turn = conversation[index];
    if (turn.speaker !== 'employee') continue;
    conversation[index] = {
      ...turn,
      metadata: {
        ...(turn.metadata || {}),
        ...(serverTiming ? { rehearsal_timing: serverTiming } : {}),
        rehearsal_client_timing: clientTiming,
      },
    };
    break;
  }
  return { ...state, conversation };
}

type WorkflowProviderProps = PropsWithChildren<{
  sessionStorageKey?: string;
  allowSessionCreation?: boolean;
  restorePreviousEmployeeSettings?: boolean;
}>;

const LEGACY_PROFILE_TEXT_DRAFT_SUFFIX = 'profile_text_draft';

function safeSessionStorage(): WorkflowDraftStorage | null {
  if (typeof window === 'undefined') return null;
  try {
    return window.sessionStorage;
  } catch {
    return null;
  }
}

function readStoredSessionId(storageKey: string): string | null {
  if (typeof window === 'undefined') return null;
  try {
    return window.localStorage.getItem(storageKey);
  } catch {
    return null;
  }
}

function persistStoredSessionId(storageKey: string, sessionId: string | null): void {
  if (typeof window === 'undefined') return;
  try {
    if (sessionId) window.localStorage.setItem(storageKey, sessionId);
    else window.localStorage.removeItem(storageKey);
  } catch {
    // The active session remains available in React state when storage is blocked.
  }
}

function legacyProfileDraftStorageKey(storageKey: string, sessionId: string | null): string {
  return `${storageKey}:${LEGACY_PROFILE_TEXT_DRAFT_SUFFIX}:${sessionId || 'pending'}`;
}

function readDraftWithLegacyMigration(
  storage: WorkflowDraftStorage | null,
  storageKey: string,
  sessionId: string | null,
): WorkflowDraft | null {
  if (!storage) return null;
  const current = readWorkflowDraft(storage, storageKey, sessionId);
  if (current !== null) return current;

  try {
    const legacyKey = legacyProfileDraftStorageKey(storageKey, sessionId);
    const profileText = storage.getItem(legacyKey);
    if (profileText === null) return null;
    const migrated: WorkflowDraft = { profileText };
    if (writeWorkflowDraft(storage, storageKey, sessionId, migrated)) {
      storage.removeItem(legacyKey);
    }
    return migrated;
  } catch {
    return null;
  }
}

function fileMetadata(file: File): WorkflowDraftFileMetadata {
  return {
    name: file.name,
    size: file.size,
    type: file.type,
    lastModified: file.lastModified,
  };
}

function employeeRecordFromSession(state: SessionState | null): EmployeeRecord | null {
  const profile = state?.employee_profile;
  if (!profile) return null;
  return {
    ...profile,
    profile,
    profile_text: profile.source_profile_text || null,
  };
}

function hasDraftField<K extends keyof WorkflowDraft>(
  draft: WorkflowDraft,
  key: K,
): draft is WorkflowDraft & Required<Pick<WorkflowDraft, K>> {
  return Object.prototype.hasOwnProperty.call(draft, key);
}

function coachTasksFromReport(report: CoachReport): CoachTaskDraft[] {
  const resultsById = new Map(report.task_results.map((result) => [result.task_id, result]));
  return coachTaskSeeds.map((seed) => {
    const result = resultsById.get(seed.task_id);
    if (!result) {
      return { ...seed, status: 'error', result: null, summary: '', score: null, error: '报告缺少该评估任务。' };
    }
    const failed = result.status === 'failed';
    return {
      ...seed,
      task_name: result.task_name || seed.task_name,
      status: failed ? 'error' : 'done',
      result,
      summary: result.summary || '',
      score: typeof result.score === 'number' ? result.score : null,
      error: failed ? result.summary || '该评估任务失败。' : null,
    };
  });
}

export function WorkflowProvider({
  children,
  sessionStorageKey = 'hr_agent_session_id',
  allowSessionCreation = true,
  restorePreviousEmployeeSettings = false,
}: WorkflowProviderProps) {
  const { language, setLanguage, translate } = useLanguage();
  const [sessionId, setSessionId] = useState<string | null>(() => readStoredSessionId(sessionStorageKey));
  const [session, setSession] = useState<SessionState | null>(null);
  const [options, setOptions] = useState<SetupOptions>(emptyOptions);
  const [profileText, setProfileTextState] = useState(DEFAULT_PROFILE_TEXT);
  const [selectedFile, setSelectedFileState] = useState<File | null>(null);
  const [selectedFileMetadata, setSelectedFileMetadataState] = useState<WorkflowDraftFileMetadata | null>(null);
  const [employeeResults, setEmployeeResults] = useState<EmployeeRecord[]>([]);
  const [selectedEmployee, setSelectedEmployee] = useState<EmployeeRecord | null>(null);
  const [selectedIntentId, setSelectedIntentIdState] = useState<string | null>(null);
  const [intentPerformanceDrafts, setIntentPerformanceDraftsState] = useState<Record<string, IntentPerformanceDraftResponse>>({});
  const [selectedPersonality, setSelectedPersonalityState] = useState<BigFivePersonality>(DEFAULT_BIG_FIVE);
  const [motiveSelection, setMotiveSelection] = useState<MotiveSelectionState>({
    hydrated: false,
    primaryMotiveId: null,
    secondaryMotiveIds: [],
  });
  const selectedPrimaryMotiveId = motiveSelection.primaryMotiveId;
  const selectedSecondaryMotiveIds = motiveSelection.secondaryMotiveIds;
  const [guidanceReport, setGuidanceReport] = useState<GuidanceReport | null>(null);
  const [guidanceSections, setGuidanceSections] = useState<GuidanceSectionDraft[]>(() => createGuidanceSections());
  const [guidanceStatus, setGuidanceStatus] = useState<WorkflowStreamStatus>('idle');
  const [coachReport, setCoachReport] = useState<CoachReport | null>(null);
  const [reportStatus, setReportStatus] = useState<WorkflowStreamStatus>('idle');
  const [coachTasks, setCoachTasks] = useState<CoachTaskDraft[]>(() => createCoachTasks());
  const [liveConversation, setLiveConversation] = useState<ConversationTurn[] | null>(null);
  const [rehearsalStreaming, setRehearsalStreaming] = useState(false);
  const [rehearsalDraft, setRehearsalDraftState] = useState('');
  const [rehearsalRuntimeNote, setRehearsalRuntimeNoteState] = useState('');
  const [bootstrapStatus, setBootstrapStatus] = useState<'idle' | 'loading' | 'ready' | 'error'>('idle');
  const [loading, setLoading] = useState({ active: false, text: '处理中...' });
  const [toast, setToast] = useState<ToastState | null>(null);
  const toastTimer = useRef<number | null>(null);
  const bootstrapPromise = useRef<Promise<void> | null>(null);
  const employeeLookupRequest = useRef(0);
  const employeeDirectory = useRef<EmployeeRecord[] | null>(null);
  const employeeDirectoryLoadedAt = useRef(0);
  const employeeDirectoryPromise = useRef<Promise<EmployeeRecord[]> | null>(null);
  const selectedEmployeeIdRef = useRef<string | null>(null);
  const preparedEmployeeIdRef = useRef<string | null>(null);
  const employeeSettingsRestoreRequest = useRef(0);
  const employeeSettingsRestorePromise = useRef<Promise<void> | null>(null);
  const bootstrapCompleted = useRef(false);
  const freshSessionPromise = useRef<Promise<SessionState> | null>(null);
  const guidanceInFlight = useRef<number | null>(null);
  const reportInFlight = useRef<number | null>(null);
  const rehearsalMessageInFlight = useRef(false);
  const draftSessionId = useRef<string | null>(sessionId);
  const workflowDraftRef = useRef<WorkflowDraft>({});
  const workflowDraftPersistEnabledRef = useRef(false);
  const workflowDraftPersistTimerRef = useRef<number | null>(null);
  const storageWarningShown = useRef(false);
  const intentPerformanceDraftsRef = useRef(intentPerformanceDrafts);
  const selectedPersonalityRef = useRef(selectedPersonality);
  const motiveSelectionRef = useRef(motiveSelection);
  const rehearsalDraftRef = useRef(rehearsalDraft);
  const rehearsalRuntimeNoteRef = useRef(rehearsalRuntimeNote);
  const sessionRef = useRef(session);
  const languageRef = useRef(language);
  const localeSyncFailureKeyRef = useRef<string | null>(null);
  const performanceLocaleRefreshRef = useRef<string | null>(null);
  const presentedLanguageRef = useRef(language);
  const localeCoordinatorRef = useRef<SessionLocaleCoordinator | null>(null);
  if (!localeCoordinatorRef.current) {
    localeCoordinatorRef.current = new SessionLocaleCoordinator(language);
  }
  const localeCoordinator = localeCoordinatorRef.current;

  sessionRef.current = session;
  languageRef.current = language;
  localeCoordinator.setDesiredLocale(language);

  useEffect(() => { intentPerformanceDraftsRef.current = intentPerformanceDrafts; }, [intentPerformanceDrafts]);
  useEffect(() => { selectedPersonalityRef.current = selectedPersonality; }, [selectedPersonality]);
  useEffect(() => { motiveSelectionRef.current = motiveSelection; }, [motiveSelection]);
  useEffect(() => { rehearsalDraftRef.current = rehearsalDraft; }, [rehearsalDraft]);
  useEffect(() => { rehearsalRuntimeNoteRef.current = rehearsalRuntimeNote; }, [rehearsalRuntimeNote]);

  const showToast = useCallback((message: string, type: 'ok' | 'error' = 'ok') => {
    setToast({ message, type });
    if (toastTimer.current) window.clearTimeout(toastTimer.current);
    toastTimer.current = window.setTimeout(() => setToast(null), 4200);
  }, []);

  const cancelWorkflowDraftPersist = useCallback(() => {
    if (workflowDraftPersistTimerRef.current === null) return;
    window.clearTimeout(workflowDraftPersistTimerRef.current);
    workflowDraftPersistTimerRef.current = null;
  }, []);

  const flushWorkflowDraftPersist = useCallback(() => {
    cancelWorkflowDraftPersist();
    if (!workflowDraftPersistEnabledRef.current) return true;
    const storage = safeSessionStorage();
    const currentDraft = workflowDraftRef.current;
    const saved = Object.keys(currentDraft).length
      ? writeWorkflowDraft(
          storage,
          sessionStorageKey,
          draftSessionId.current,
          currentDraft,
        )
      : removeWorkflowDraft(storage, sessionStorageKey, draftSessionId.current);
    if (!saved && !storageWarningShown.current) {
      storageWarningShown.current = true;
      showToast('浏览器草稿存储不可用，本页编辑仍会保留到当前页面关闭。', 'error');
    }
    return saved;
  }, [cancelWorkflowDraftPersist, sessionStorageKey, showToast]);

  const scheduleWorkflowDraftPersist = useCallback(() => {
    cancelWorkflowDraftPersist();
    workflowDraftPersistTimerRef.current = window.setTimeout(() => {
      workflowDraftPersistTimerRef.current = null;
      flushWorkflowDraftPersist();
    }, WORKFLOW_DRAFT_PERSIST_DELAY_MS);
  }, [cancelWorkflowDraftPersist, flushWorkflowDraftPersist]);

  const replaceWorkflowDraft = useCallback((next: WorkflowDraft) => {
    workflowDraftRef.current = next;
    workflowDraftPersistEnabledRef.current = true;
    scheduleWorkflowDraftPersist();
  }, [scheduleWorkflowDraftPersist]);

  const patchWorkflowDraft = useCallback((patch: Partial<WorkflowDraft>) => {
    replaceWorkflowDraft({ ...workflowDraftRef.current, ...patch });
  }, [replaceWorkflowDraft]);

  const clearWorkflowDraftFields = useCallback((fields: Array<keyof WorkflowDraft>) => {
    const next = { ...workflowDraftRef.current };
    fields.forEach((field) => delete next[field]);
    replaceWorkflowDraft(next);
  }, [replaceWorkflowDraft]);

  const applyLocaleSession = useCallback((nextSession: SessionState, fallbackLocale: SessionLocale) => {
    const normalizedSession: SessionState = {
      ...nextSession,
      locale: nextSession.locale || fallbackLocale,
    };
    sessionRef.current = normalizedSession;
    setSession((current) => current?.session_id === normalizedSession.session_id
      ? normalizedSession
      : current);

    // Guidance and review outputs are generated in the session locale. The locale endpoint
    // invalidates their server-side ids, so the matching client caches must be invalidated too.
    setGuidanceReport(null);
    setGuidanceSections(createGuidanceSections());
    setGuidanceStatus('idle');
    setCoachReport(null);
    setCoachTasks(createCoachTasks());
    setReportStatus('idle');
    setLiveConversation(null);

    intentPerformanceDraftsRef.current = {};
    setIntentPerformanceDraftsState({});
    patchWorkflowDraft({ intentPerformanceDrafts: {} });
    return normalizedSession;
  }, [patchWorkflowDraft]);

  const synchronizeSessionLocale = useCallback(async (): Promise<SessionState | null> => {
    const requestKey = `${sessionRef.current?.session_id || ''}:${languageRef.current}`;
    try {
      const synchronized = await localeCoordinator.synchronize({
        getSession: () => sessionRef.current,
        updateLocale: api.updateSessionLocale,
        refreshSession: api.getSession,
        applySession: applyLocaleSession,
        rollbackLanguage: (rollbackLocale) => {
          languageRef.current = rollbackLocale;
          setLanguage(rollbackLocale);
        },
      });
      localeSyncFailureKeyRef.current = null;
      return synchronized;
    } catch (error) {
      if (localeSyncFailureKeyRef.current !== requestKey) {
        localeSyncFailureKeyRef.current = requestKey;
        const sourceError = error instanceof SessionLocaleSyncError ? error.cause : error;
        showToast(
          userFacingErrorMessage(sourceError, undefined, '语言设置同步失败，已恢复为会话原语言，请重试。'),
          'error',
        );
      }
      throw error;
    }
  }, [applyLocaleSession, localeCoordinator, setLanguage, showToast]);

  useEffect(() => {
    if (presentedLanguageRef.current === language) return;
    presentedLanguageRef.current = language;

    // Generated UI belongs to the locale that created it. Hide it as soon as
    // the selector changes instead of leaving mixed-language content visible
    // while the session-locale PATCH is still in flight.
    setGuidanceReport(null);
    setGuidanceSections(createGuidanceSections());
    setGuidanceStatus('idle');
    setCoachReport(null);
    setCoachTasks(createCoachTasks());
    setReportStatus('idle');
    setLiveConversation(null);
    setToast(null);
    if (toastTimer.current !== null) {
      window.clearTimeout(toastTimer.current);
      toastTimer.current = null;
    }
  }, [language]);

  useEffect(() => {
    void synchronizeSessionLocale().catch(() => undefined);
  }, [language, session?.locale, session?.session_id, synchronizeSessionLocale]);

  useEffect(() => {
    const activeSession = session;
    const activeLocale = activeSession?.locale || 'zh-CN';
    const activeIntent = activeSession?.intent;
    const intentId = activeIntent?.intent_id || activeIntent?.id || '';
    const performanceLocale = activeIntent?.performance_locale || 'zh-CN';
    if (
      !activeSession
      || activeLocale !== language
      || !intentId
      || performanceLocale === activeLocale
    ) {
      performanceLocaleRefreshRef.current = null;
      return;
    }

    const refreshKey = `${activeSession.session_id}:${intentId}:${activeLocale}`;
    if (performanceLocaleRefreshRef.current === refreshKey) return;
    performanceLocaleRefreshRef.current = refreshKey;
    let cancelled = false;
    const token = localeCoordinator.capture(activeSession);

    void api.generateIntentPerformanceDraft(activeSession.session_id, intentId)
      .then((draft) => {
        if (
          cancelled
          || !localeCoordinator.isCurrent(token, sessionRef.current)
          || draft.locale !== token.locale
        ) return;
        const nextDrafts = {
          ...intentPerformanceDraftsRef.current,
          [intentId]: draft,
        };
        intentPerformanceDraftsRef.current = nextDrafts;
        setIntentPerformanceDraftsState(nextDrafts);
        setSelectedIntentIdState(intentId);
        patchWorkflowDraft({
          selectedIntentId: intentId,
          intentPerformanceDrafts: nextDrafts,
        });
        showToast('语言已切换，员工表现初稿已按新语言重新生成，请返回沟通意图页确认。');
      })
      .catch((error) => {
        if (cancelled || !localeCoordinator.isCurrent(token, sessionRef.current)) return;
        showToast(
          userFacingErrorMessage(
            error,
            undefined,
            '语言已切换，但员工表现初稿未能重新生成，请返回沟通意图页重试。',
          ),
          'error',
        );
      });

    return () => {
      cancelled = true;
    };
  }, [
    patchWorkflowDraft,
    language,
    localeCoordinator,
    session?.intent?.id,
    session?.intent?.intent_id,
    session?.intent?.performance_locale,
    session?.locale,
    session?.session_id,
    showToast,
  ]);

  const clearDownstreamState = useCallback(() => {
    setGuidanceReport(null);
    setGuidanceSections(createGuidanceSections());
    setGuidanceStatus('idle');
    setCoachReport(null);
    setReportStatus('idle');
    setCoachTasks(createCoachTasks());
    setLiveConversation(null);
    setRehearsalStreaming(false);
    rehearsalDraftRef.current = '';
    rehearsalRuntimeNoteRef.current = '';
    setRehearsalDraftState('');
    setRehearsalRuntimeNoteState('');
    clearWorkflowDraftFields(['rehearsalDraft', 'runtimeNote']);
  }, [clearWorkflowDraftFields]);

  useEffect(() => {
    const flushWhenHidden = () => {
      if (document.visibilityState === 'hidden') flushWorkflowDraftPersist();
    };
    window.addEventListener('pagehide', flushWorkflowDraftPersist);
    document.addEventListener('visibilitychange', flushWhenHidden);
    return () => {
      window.removeEventListener('pagehide', flushWorkflowDraftPersist);
      document.removeEventListener('visibilitychange', flushWhenHidden);
      // Do not flush on an ordinary provider unmount: logout intentionally clears
      // browser storage and must not restore the previous employee's draft.
      cancelWorkflowDraftPersist();
    };
  }, [cancelWorkflowDraftPersist, flushWorkflowDraftPersist]);

  const setProfileText = useCallback((text: string) => {
    setProfileTextState(text);
    patchWorkflowDraft({ profileText: text });
  }, [patchWorkflowDraft]);

  const setSelectedFile = useCallback((file: File | null) => {
    const metadata = file ? fileMetadata(file) : null;
    setSelectedFileState(file);
    setSelectedFileMetadataState(metadata);
    patchWorkflowDraft({ selectedFile: metadata });
  }, [patchWorkflowDraft]);

  const discardSelectedFileMetadata = useCallback(() => {
    setSelectedFileState(null);
    setSelectedFileMetadataState(null);
    patchWorkflowDraft({ selectedFile: null });
  }, [patchWorkflowDraft]);

  const setSelectedEmployeeDraft = useCallback((record: EmployeeRecord | null) => {
    selectedEmployeeIdRef.current = String(record?.employee_id || '').trim() || null;
    setSelectedEmployee(record);
    patchWorkflowDraft({ selectedEmployee: record });
  }, [patchWorkflowDraft]);

  const setSelectedIntentId = useCallback((id: string) => {
    setSelectedIntentIdState(id);
    patchWorkflowDraft({ selectedIntentId: id });
  }, [patchWorkflowDraft]);

  const setIntentPerformanceDrafts = useCallback((
    update: SetStateAction<Record<string, IntentPerformanceDraftResponse>>,
  ) => {
    const next = typeof update === 'function'
      ? update(intentPerformanceDraftsRef.current)
      : update;
    intentPerformanceDraftsRef.current = next;
    setIntentPerformanceDraftsState(next);
    patchWorkflowDraft({ intentPerformanceDrafts: next });
  }, [patchWorkflowDraft]);

  const updatePersonalityDimension = useCallback((dimension: keyof BigFivePersonality, value: number) => {
    const next = { ...selectedPersonalityRef.current, [dimension]: clampPersonalityScore(value) };
    selectedPersonalityRef.current = next;
    setSelectedPersonalityState(next);
    patchWorkflowDraft({ selectedPersonality: next });
  }, [patchWorkflowDraft]);

  const setSelectedPrimaryMotiveId = useCallback((id: string) => {
    if (!id) return;
    const current = motiveSelectionRef.current;
    const next = {
      hydrated: true,
      primaryMotiveId: id,
      secondaryMotiveIds: normalizeSecondaryMotiveIds(current.secondaryMotiveIds, id),
    };
    motiveSelectionRef.current = next;
    setMotiveSelection(next);
    patchWorkflowDraft({
      selectedPrimaryMotiveId: next.primaryMotiveId,
      selectedSecondaryMotiveIds: next.secondaryMotiveIds,
    });
    clearDownstreamState();
  }, [clearDownstreamState, patchWorkflowDraft]);

  const setSelectedSecondaryMotiveIds = useCallback((update: SetStateAction<string[]>) => {
    const current = motiveSelectionRef.current;
    const requested = typeof update === 'function' ? update(current.secondaryMotiveIds) : update;
    const next = {
      ...current,
      hydrated: true,
      secondaryMotiveIds: normalizeSecondaryMotiveIds(requested, current.primaryMotiveId),
    };
    motiveSelectionRef.current = next;
    setMotiveSelection(next);
    patchWorkflowDraft({ selectedSecondaryMotiveIds: next.secondaryMotiveIds });
    clearDownstreamState();
  }, [clearDownstreamState, patchWorkflowDraft]);

  const setRehearsalDraft = useCallback((update: SetStateAction<string>) => {
    const next = typeof update === 'function' ? update(rehearsalDraftRef.current) : update;
    if (next === rehearsalDraftRef.current) return;
    rehearsalDraftRef.current = next;
    setRehearsalDraftState(next);
    if (next) patchWorkflowDraft({ rehearsalDraft: next });
    else clearWorkflowDraftFields(['rehearsalDraft']);
  }, [clearWorkflowDraftFields, patchWorkflowDraft]);

  const setRehearsalRuntimeNote = useCallback((update: SetStateAction<string>) => {
    const next = typeof update === 'function' ? update(rehearsalRuntimeNoteRef.current) : update;
    rehearsalRuntimeNoteRef.current = next;
    setRehearsalRuntimeNoteState(next);
    if (next) patchWorkflowDraft({ runtimeNote: next });
    else clearWorkflowDraftFields(['runtimeNote']);
  }, [clearWorkflowDraftFields, patchWorkflowDraft]);

  const selectedEmployeeProfile = useMemo(
    () => selectedEmployee ? profileFromEmployeeRecord(selectedEmployee) : null,
    [selectedEmployee],
  );
  const employeeSelectionChanged = useMemo(
    () => Boolean(
      selectedEmployeeProfile && !sameProfile(selectedEmployeeProfile, session?.employee_profile),
    ),
    [selectedEmployeeProfile, session?.employee_profile],
  );
  const baseDisplayedProfile = selectedEmployeeProfile || session?.employee_profile || null;
  const displayedProfile = useMemo(() => baseDisplayedProfile ? {
    ...baseDisplayedProfile,
    supplemental_info: employeeSelectionChanged
      ? baseDisplayedProfile.supplemental_info || null
      : session?.supplemental_info || baseDisplayedProfile.supplemental_info || null,
  } : null, [baseDisplayedProfile, employeeSelectionChanged, session?.supplemental_info]);
  const effectiveSelectedIntentId = employeeSelectionChanged
    ? selectedIntentId
    : selectedIntentId || sessionIntentId(session);
  const effectiveSelectedPersonality = useMemo(
    () => normalizeBigFive(selectedPersonality || session?.personality),
    [selectedPersonality, session?.personality],
  );
  const effectiveSelectedPrimaryMotiveId = motiveSelection.hydrated
    ? selectedPrimaryMotiveId
    : sessionPrimaryMotiveId(session);
  const effectiveSelectedSecondaryMotiveIds = motiveSelection.hydrated
    ? selectedSecondaryMotiveIds
    : sessionSecondaryMotiveIds(session);

  const resetEmployeeDependentState = useCallback(() => {
    setSelectedIntentIdState(null);
    intentPerformanceDraftsRef.current = {};
    setIntentPerformanceDraftsState({});
    const personality = normalizeBigFive(options.default_big_five);
    selectedPersonalityRef.current = personality;
    setSelectedPersonalityState(personality);
    const motives = {
      hydrated: true,
      primaryMotiveId: null,
      secondaryMotiveIds: [],
    };
    motiveSelectionRef.current = motives;
    setMotiveSelection(motives);
    clearDownstreamState();
    patchWorkflowDraft({
      selectedIntentId: null,
      intentPerformanceDrafts: {},
      selectedPersonality: personality,
      selectedPrimaryMotiveId: null,
      selectedSecondaryMotiveIds: [],
    });
  }, [clearDownstreamState, options.default_big_five, patchWorkflowDraft]);

  const syncSimulationSelection = useCallback((state: SessionState | null, loadedOptions: SetupOptions = options) => {
    const personality = normalizeBigFive(state?.personality || loadedOptions.default_big_five);
    selectedPersonalityRef.current = personality;
    setSelectedPersonalityState(personality);

    const primary = sessionPrimaryMotiveId(state);
    const secondary = sessionSecondaryMotiveIds(state);
    const motives = {
      hydrated: true,
      primaryMotiveId: primary,
      secondaryMotiveIds: secondary,
    };
    motiveSelectionRef.current = motives;
    setMotiveSelection(motives);
  }, [options]);

  const resetWorkspaceState = useCallback(() => {
    employeeSettingsRestoreRequest.current += 1;
    employeeSettingsRestorePromise.current = null;
    selectedEmployeeIdRef.current = null;
    preparedEmployeeIdRef.current = null;
    setProfileTextState(DEFAULT_PROFILE_TEXT);
    setSelectedFileState(null);
    setSelectedFileMetadataState(null);
    setEmployeeResults([]);
    setSelectedEmployee(null);
    resetEmployeeDependentState();
    replaceWorkflowDraft({});
  }, [replaceWorkflowDraft, resetEmployeeDependentState]);

  const hydrateWorkflowDraft = useCallback((loadedSession: SessionState, loadedOptions: SetupOptions) => {
    const storage = safeSessionStorage();
    const stored = readDraftWithLegacyMigration(storage, sessionStorageKey, loadedSession.session_id) || {};
    const draft = { ...stored, ...workflowDraftRef.current };
    draftSessionId.current = loadedSession.session_id;

    const hydratedEmployee = hasDraftField(draft, 'selectedEmployee')
      ? draft.selectedEmployee
      : employeeRecordFromSession(loadedSession);
    selectedEmployeeIdRef.current = String(hydratedEmployee?.employee_id || '').trim() || null;
    setSelectedEmployee(hydratedEmployee);
    setProfileTextState(hasDraftField(draft, 'profileText') ? draft.profileText : DEFAULT_PROFILE_TEXT);
    setSelectedFileState(null);
    setSelectedFileMetadataState(hasDraftField(draft, 'selectedFile') ? draft.selectedFile : null);
    setSelectedIntentIdState(hasDraftField(draft, 'selectedIntentId') ? draft.selectedIntentId : null);

    const performanceDrafts = hasDraftField(draft, 'intentPerformanceDrafts')
      ? draft.intentPerformanceDrafts
      : {};
    intentPerformanceDraftsRef.current = performanceDrafts;
    setIntentPerformanceDraftsState(performanceDrafts);

    if (hasDraftField(draft, 'selectedPersonality') && draft.selectedPersonality) {
      const personality = normalizeBigFive(draft.selectedPersonality);
      selectedPersonalityRef.current = personality;
      setSelectedPersonalityState(personality);
    }

    if (
      hasDraftField(draft, 'selectedPrimaryMotiveId')
      || hasDraftField(draft, 'selectedSecondaryMotiveIds')
    ) {
      const primary = hasDraftField(draft, 'selectedPrimaryMotiveId')
        ? draft.selectedPrimaryMotiveId
        : sessionPrimaryMotiveId(loadedSession);
      const secondary = normalizeSecondaryMotiveIds(
        hasDraftField(draft, 'selectedSecondaryMotiveIds')
          ? draft.selectedSecondaryMotiveIds
          : sessionSecondaryMotiveIds(loadedSession),
        primary,
      );
      const motives = { hydrated: true, primaryMotiveId: primary, secondaryMotiveIds: secondary };
      motiveSelectionRef.current = motives;
      setMotiveSelection(motives);
    }

    const message = hasDraftField(draft, 'rehearsalDraft') ? draft.rehearsalDraft : '';
    rehearsalDraftRef.current = message;
    setRehearsalDraftState(message);
    const runtimeNote = hasDraftField(draft, 'runtimeNote') ? draft.runtimeNote : '';
    rehearsalRuntimeNoteRef.current = runtimeNote;
    setRehearsalRuntimeNoteState(runtimeNote);
    replaceWorkflowDraft(draft);

    if (!loadedSession.personality && !hasDraftField(draft, 'selectedPersonality')) {
      const personality = normalizeBigFive(loadedOptions.default_big_five);
      selectedPersonalityRef.current = personality;
      setSelectedPersonalityState(personality);
    }
  }, [replaceWorkflowDraft, sessionStorageKey]);

  const runTask = useCallback(async <T,>(label: string, fn: () => Promise<T>): Promise<T> => {
    setLoading({ active: true, text: label });
    try {
      return await fn();
    } catch (error) {
      const err = error as Error & { name?: string };
      showToast(
        err?.name === 'AbortError'
          ? '请求超时，请稍后重试。'
          : userFacingErrorMessage(error, undefined, '操作未完成，请稍后重试。'),
        'error',
      );
      throw error;
    } finally {
      setLoading({ active: false, text: '处理中...' });
    }
  }, [showToast]);

  const buildSetupSnapshot = useCallback((base: SessionState | null, overrides: SetupSnapshot = {}): SetupSnapshot => ({
    profile: overrides.profile !== undefined ? overrides.profile : base?.employee_profile || displayedProfile,
    intentId: overrides.intentId !== undefined ? overrides.intentId : selectedIntentId || sessionIntentId(base),
    intentPerformanceContext: overrides.intentPerformanceContext !== undefined
      ? overrides.intentPerformanceContext
      : sessionIntentPerformanceContext(base),
    intentPerformanceItems: overrides.intentPerformanceItems !== undefined
      ? overrides.intentPerformanceItems
      : base?.intent?.performance_items?.length
        ? base.intent.performance_items
        : null,
    personality: overrides.personality !== undefined ? overrides.personality : base?.personality || selectedPersonality,
    primaryMotiveId: overrides.primaryMotiveId !== undefined
      ? overrides.primaryMotiveId
      : motiveSelection.hydrated ? selectedPrimaryMotiveId : sessionPrimaryMotiveId(base),
    secondaryMotiveIds: overrides.secondaryMotiveIds !== undefined
      ? overrides.secondaryMotiveIds
      : motiveSelection.hydrated ? selectedSecondaryMotiveIds : sessionSecondaryMotiveIds(base),
  }), [
    displayedProfile,
    motiveSelection.hydrated,
    selectedIntentId,
    selectedPersonality,
    selectedPrimaryMotiveId,
    selectedSecondaryMotiveIds,
  ]);

  const createSessionFromSetupSnapshot = useCallback(async (snapshot: SetupSnapshot): Promise<SessionState> => {
    if (!allowSessionCreation) throw new Error('当前测试会话不存在，请从管理员测试入口重新创建。');
    if (snapshot.intentId && !snapshot.intentPerformanceItems?.length) {
      throw new Error('该会话使用已废弃的当前表现格式，请返回沟通意图页重新生成。');
    }
    const created = await api.createSession(languageRef.current);

    let next = created;
    if (snapshot.profile) {
      next = await api.confirmProfile(created.session_id, snapshot.profile);
    }
    if (snapshot.intentId) {
      next = await api.confirmIntent(
        created.session_id,
        snapshot.intentId,
        snapshot.intentPerformanceItems!,
      );
    }
    if (snapshot.personality && snapshot.primaryMotiveId && validSecondaryMotiveCount(snapshot.secondaryMotiveIds)) {
      next = await api.confirmSimulation(created.session_id, normalizeBigFive(snapshot.personality), snapshot.primaryMotiveId, snapshot.secondaryMotiveIds);
    }
    if (snapshot.profile && snapshot.intentId && snapshot.personality && snapshot.primaryMotiveId && validSecondaryMotiveCount(snapshot.secondaryMotiveIds)) {
      next = await api.completeSetup(created.session_id);
    }

    flushWorkflowDraftPersist();
    const previousDraftSessionId = draftSessionId.current;
    const storage = safeSessionStorage();
    readDraftWithLegacyMigration(storage, sessionStorageKey, previousDraftSessionId);
    moveWorkflowDraft(storage, sessionStorageKey, previousDraftSessionId, created.session_id);
    draftSessionId.current = created.session_id;
    persistStoredSessionId(sessionStorageKey, created.session_id);
    setSessionId(created.session_id);
    setSession(next);
    syncSimulationSelection(next);
    clearDownstreamState();
    replaceWorkflowDraft({ ...workflowDraftRef.current });
    return next;
  }, [
    allowSessionCreation,
    clearDownstreamState,
    flushWorkflowDraftPersist,
    replaceWorkflowDraft,
    sessionStorageKey,
    syncSimulationSelection,
  ]);

  const beginFreshSessionFromSnapshot = useCallback((snapshot: SetupSnapshot) => {
    if (freshSessionPromise.current) return freshSessionPromise.current;
    const promise = createSessionFromSetupSnapshot(snapshot).catch((error) => {
      showToast(userFacingErrorMessage(error, undefined, '新会话创建失败，请稍后重试。'), 'error');
      throw error;
    });
    freshSessionPromise.current = promise;
    void promise.finally(() => {
      if (freshSessionPromise.current === promise) freshSessionPromise.current = null;
    });
    return promise;
  }, [createSessionFromSetupSnapshot, showToast]);

  const ensureSession = useCallback(async (): Promise<SessionState> => {
    if (freshSessionPromise.current) {
      const fresh = await freshSessionPromise.current;
      sessionRef.current = fresh;
      const synchronized = await synchronizeSessionLocale();
      return synchronized || fresh;
    }
    let invalidatedAfterBootstrap = false;
    let recovery;
    try {
      recovery = await recoverWorkflowSession({
        storedSessionId: sessionId,
        allowSessionCreation,
        loadSession: api.getSession,
        createSession: () => api.createSession(languageRef.current),
        isInvalidStoredSession: shouldInvalidateStoredSession,
        onInvalidated: (invalidatedSessionId) => {
          cancelWorkflowDraftPersist();
          workflowDraftPersistEnabledRef.current = false;
          persistStoredSessionId(sessionStorageKey, null);
          const storage = safeSessionStorage();
          removeWorkflowDraft(storage, sessionStorageKey, invalidatedSessionId);
          try {
            storage?.removeItem(legacyProfileDraftStorageKey(sessionStorageKey, invalidatedSessionId));
          } catch {
            // The invalid server session is still discarded from in-memory state.
          }
          workflowDraftRef.current = {};
          setSession(null);
          setSessionId(null);
          invalidatedAfterBootstrap = bootstrapCompleted.current;
          if (invalidatedAfterBootstrap) resetWorkspaceState();
        },
      });
    } catch (error) {
      if (error instanceof WorkflowSessionCreationDisabledError) {
        throw new Error('当前测试会话不存在，请从管理员测试入口重新创建。');
      }
      throw error;
    }

    if (recovery.source === 'loaded') {
      sessionRef.current = recovery.session;
      setSession(recovery.session);
      const synchronized = await synchronizeSessionLocale();
      return synchronized || recovery.session;
    }

    const created = recovery.session;
    const storage = safeSessionStorage();
    if (recovery.invalidatedSessionId === null) {
      flushWorkflowDraftPersist();
      readDraftWithLegacyMigration(storage, sessionStorageKey, null);
      moveWorkflowDraft(storage, sessionStorageKey, null, created.session_id);
    }
    draftSessionId.current = created.session_id;
    persistStoredSessionId(sessionStorageKey, created.session_id);
    setSessionId(created.session_id);
    sessionRef.current = created;
    setSession(created);
    if (invalidatedAfterBootstrap) {
      resetWorkspaceState();
      throw new Error('当前会话已失效，已创建新会话，请重新执行刚才的操作。');
    }
    const synchronized = await synchronizeSessionLocale();
    return synchronized || created;
  }, [
    allowSessionCreation,
    cancelWorkflowDraftPersist,
    flushWorkflowDraftPersist,
    resetWorkspaceState,
    sessionId,
    sessionStorageKey,
    synchronizeSessionLocale,
  ]);

  const ensureSessionLocale = useCallback(async (): Promise<void> => {
    await ensureSession();
  }, [ensureSession]);

  const beginLocaleOperation = useCallback(async (): Promise<LocaleOperation> => {
    const activeSession = await ensureSession();
    const token = localeCoordinator.capture(activeSession);
    if (!localeCoordinator.isCurrent(token, sessionRef.current)) {
      throw new StaleLocaleResultError();
    }
    return { session: activeSession, token };
  }, [ensureSession, localeCoordinator]);

  const isLocaleOperationCurrent = useCallback((
    token: SessionLocaleOperationToken,
    payload?: LocaleBoundPayload | null,
  ) => (
    localeCoordinator.isCurrent(token, sessionRef.current)
    && (!payload || payload.locale === token.locale)
  ), [localeCoordinator]);

  const assertLocaleOperationCurrent = useCallback((
    token: SessionLocaleOperationToken,
    payload?: LocaleBoundPayload | null,
  ) => {
    if (!isLocaleOperationCurrent(token, payload)) throw new StaleLocaleResultError();
  }, [isLocaleOperationCurrent]);

  const startNewSession = useCallback(async (): Promise<SessionState> => {
    return await runTask('创建绩效反馈会话', async () => {
      if (!allowSessionCreation) throw new Error('请从管理员测试入口创建新的测试会话。');
      const created = await api.createSession(languageRef.current);
      const storage = safeSessionStorage();
      cancelWorkflowDraftPersist();
      workflowDraftPersistEnabledRef.current = false;
      clearWorkflowDrafts(storage, sessionStorageKey);
      try {
        storage?.removeItem(legacyProfileDraftStorageKey(sessionStorageKey, draftSessionId.current));
        storage?.removeItem(legacyProfileDraftStorageKey(sessionStorageKey, null));
      } catch {
        // The new in-memory workspace remains usable when storage is blocked.
      }
      draftSessionId.current = created.session_id;
      persistStoredSessionId(sessionStorageKey, created.session_id);
      setSessionId(created.session_id);
      setSession(created);
      resetWorkspaceState();
      return created;
    });
  }, [
    allowSessionCreation,
    cancelWorkflowDraftPersist,
    resetWorkspaceState,
    runTask,
    sessionStorageKey,
  ]);

  const loadSetupOptions = useCallback(async () => {
    if (options.intents.length) return options;
    const loaded = await api.setupOptions();
    setOptions(loaded);
    return loaded;
  }, [options]);


  const loadEmployeeDirectory = useCallback((): Promise<EmployeeRecord[]> => {
    const cached = employeeDirectory.current;
    if (cached && Date.now() - employeeDirectoryLoadedAt.current < EMPLOYEE_DIRECTORY_REFRESH_MS) {
      return Promise.resolve(cached);
    }
    if (employeeDirectoryPromise.current) return employeeDirectoryPromise.current;

    let task: Promise<EmployeeRecord[]>;
    task = api.searchEmployees(buildEmployeeSearchParams(''))
      .then((result) => {
        const items = result.items || [];
        employeeDirectory.current = items;
        employeeDirectoryLoadedAt.current = Date.now();
        return items;
      })
      .finally(() => {
        if (employeeDirectoryPromise.current === task) employeeDirectoryPromise.current = null;
      });
    employeeDirectoryPromise.current = task;
    return task;
  }, []);

  const restoreLatestEmployeeSettings = useCallback((record: EmployeeRecord): Promise<void> => {
    const employeeId = String(record.employee_id || '').trim();
    const requestId = ++employeeSettingsRestoreRequest.current;
    preparedEmployeeIdRef.current = employeeId || null;
    if (!employeeId || !restorePreviousEmployeeSettings) {
      employeeSettingsRestorePromise.current = null;
      return Promise.resolve();
    }

    let task: Promise<void>;
    task = api.getLatestEmployeeSetupSettings(employeeId)
      .then((response) => {
        if (
          requestId !== employeeSettingsRestoreRequest.current
          || selectedEmployeeIdRef.current !== employeeId
          || response.employee_id !== employeeId
        ) return;

        const restored = normalizeLatestEmployeeSetupSettings(response);
        if (!restored.found) return;

        setProfileTextState(restored.supplementalInfo);

        const personality = restored.personality
          ? normalizeBigFive(restored.personality)
          : selectedPersonalityRef.current;
        selectedPersonalityRef.current = personality;
        setSelectedPersonalityState(personality);

        const motives = {
          hydrated: true,
          primaryMotiveId: restored.primaryMotiveId,
          secondaryMotiveIds: normalizeSecondaryMotiveIds(
            restored.secondaryMotiveIds,
            restored.primaryMotiveId,
          ),
        };
        motiveSelectionRef.current = motives;
        setMotiveSelection(motives);
        patchWorkflowDraft({
          profileText: restored.supplementalInfo,
          selectedPersonality: personality,
          selectedPrimaryMotiveId: motives.primaryMotiveId,
          selectedSecondaryMotiveIds: motives.secondaryMotiveIds,
        });
        showToast('已恢复该员工上次填写的背景信息、人格与诉求。');
      })
      .catch((error: unknown) => {
        if (
          requestId === employeeSettingsRestoreRequest.current
          && selectedEmployeeIdRef.current === employeeId
        ) {
          showToast(
            userFacingErrorMessage(error, undefined, '读取该员工上次设置失败，已保留当前设置。'),
            'error',
          );
        }
      })
      .finally(() => {
        if (employeeSettingsRestorePromise.current === task) {
          employeeSettingsRestorePromise.current = null;
        }
      });
    employeeSettingsRestorePromise.current = task;
    return task;
  }, [patchWorkflowDraft, restorePreviousEmployeeSettings, showToast]);

  const bootstrap = useCallback((): Promise<void> => {
    if (bootstrapCompleted.current) return Promise.resolve();
    if (bootstrapPromise.current) return bootstrapPromise.current;

    setBootstrapStatus('loading');
    void loadEmployeeDirectory().catch(() => undefined);
    const task = Promise.all([
      ensureSession(),
      loadSetupOptions(),
    ])
      .then(([loadedSession, loadedOptions]) => {
        syncSimulationSelection(loadedSession, loadedOptions);
        hydrateWorkflowDraft(loadedSession, loadedOptions);
        bootstrapCompleted.current = true;
        setBootstrapStatus('ready');
      })
      .catch((error) => {
        const err = error as Error & { name?: string };
        setBootstrapStatus('error');
        showToast(
          err?.name === 'AbortError'
            ? '请求超时，请稍后重试。'
            : userFacingErrorMessage(error, undefined, '页面数据载入失败，请刷新后重试。'),
          'error',
        );
        throw error;
      });

    bootstrapPromise.current = task;
    void task.then(
      () => {
        if (bootstrapPromise.current === task) bootstrapPromise.current = null;
      },
      () => {
        if (bootstrapPromise.current === task) bootstrapPromise.current = null;
      },
    );
    return task;
  }, [ensureSession, hydrateWorkflowDraft, loadEmployeeDirectory, loadSetupOptions, showToast, syncSimulationSelection]);

  const lookupEmployee = useCallback(async (
    query: string,
    lookupOptions: EmployeeLookupOptions = {},
  ) => {
    const safeQuery = query.trim();
    const requestId = ++employeeLookupRequest.current;
    const {
      autoSelectSingle = true,
      silent = false,
      signal,
    } = lookupOptions;
    const applyMatches = (directory: readonly EmployeeRecord[]) => {
      if (requestId !== employeeLookupRequest.current || signal?.aborted) return;
      const items = filterEmployeeRecords(directory, safeQuery);
      setEmployeeResults(items);
      const single = autoSelectSingle && safeQuery && items.length === 1 ? items[0] : null;
      if (single) {
        const matchedProfile = profileFromEmployeeRecord(single);
        const changed = !sameProfile(matchedProfile, session?.employee_profile);
        setSelectedEmployeeDraft(single);
        setProfileText('');
        setSelectedFile(null);
        setEmployeeResults([]);
        if (changed) resetEmployeeDependentState();
        void restoreLatestEmployeeSettings(single);
      } else if (autoSelectSingle && safeQuery) {
        setSelectedEmployeeDraft(null);
      }
      if (!items.length && safeQuery && !silent) {
        throw new Error('未匹配到员工，请改用手动输入或上传资料。');
      }
      if (safeQuery && !silent) {
        showToast(single ? '已匹配员工信息' : '请选择匹配结果');
      }
    };
    const search = async () => {
      const cached = employeeDirectory.current;
      if (cached) {
        const cacheIsStale = (
          Date.now() - employeeDirectoryLoadedAt.current
          >= EMPLOYEE_DIRECTORY_REFRESH_MS
        );
        const cachedMatches = filterEmployeeRecords(cached, safeQuery);
        if (shouldAwaitFreshEmployeeDirectory({
          cacheIsStale,
          cachedMatchCount: cachedMatches.length,
          query: safeQuery,
          silent,
        })) {
          applyMatches(await loadEmployeeDirectory());
          return;
        }
        applyMatches(cached);
        if (cacheIsStale) {
          void loadEmployeeDirectory().then((fresh) => {
            if (silent) applyMatches(fresh);
          }).catch(() => undefined);
        }
        return;
      }
      applyMatches(await loadEmployeeDirectory());
    };

    if (safeQuery && !silent) {
      await runTask('匹配员工信息', search);
      return;
    }

    try {
      await search();
    } catch (error) {
      if (requestId !== employeeLookupRequest.current || signal?.aborted) return;
      if (!silent) {
        showToast(userFacingErrorMessage(error, undefined, '员工列表载入失败。'), 'error');
      }
      throw error;
    }
  }, [loadEmployeeDirectory, resetEmployeeDependentState, restoreLatestEmployeeSettings, runTask, session?.employee_profile, setProfileText, setSelectedEmployeeDraft, setSelectedFile, showToast]);

  const selectEmployee = useCallback((record: EmployeeRecord) => {
    const matchedProfile = profileFromEmployeeRecord(record);
    const changed = !sameProfile(matchedProfile, session?.employee_profile);
    setSelectedEmployeeDraft(record);
    setProfileText('');
    setSelectedFile(null);
    setEmployeeResults([]);
    if (changed) resetEmployeeDependentState();
    void restoreLatestEmployeeSettings(record);
    showToast(changed ? '员工信息已选择，确认后将开启新的会话。' : '员工信息已选择。');
  }, [resetEmployeeDependentState, restoreLatestEmployeeSettings, session?.employee_profile, setProfileText, setSelectedEmployeeDraft, setSelectedFile, showToast]);

  const confirmProfile = useCallback(async (): Promise<boolean> => {
    return await runTask('确认员工信息', async () => {
      if (selectedFileMetadata && !selectedFile) {
        throw new Error('请重新选择或忽略上次文件。');
      }
      await employeeSettingsRestorePromise.current;
      const activeSession = await ensureSession();
      const manualSupplementalText = composeProfileText(selectedEmployee, profileText);
      if (!manualSupplementalText && !selectedFile && !selectedEmployee && !activeSession.employee_profile) {
        throw new Error('请先匹配员工、输入额外提供的信息或上传参考资料。');
      }

      const selectedProfile = selectedEmployee ? profileFromEmployeeRecord(selectedEmployee) : null;
      const profileChanged = selectedProfile ? !sameProfile(selectedProfile, activeSession.employee_profile) : false;
      const needsFreshSession = profileChanged && Boolean(activeSession.employee_profile || hasDownstreamProgress(activeSession));

      let updated: SessionState = activeSession;
      if (selectedProfile) {
        const dbText = employeeRecordText(selectedEmployee);
        selectedProfile.source_profile_text = dbText || selectedProfile.source_profile_text || null;
        selectedProfile.supplemental_info = null;

        if (needsFreshSession) {
          updated = await beginFreshSessionFromSnapshot({ profile: selectedProfile });
        } else {
          updated = await api.confirmProfile(activeSession.session_id, selectedProfile);
        }
      }

      let supplementalSaved = false;
      if (selectedFile) {
        const formData = new FormData();
        formData.append('file', selectedFile);
        formData.append('session_id', updated.session_id);
        await api.uploadDocument(formData);
        supplementalSaved = true;
      }
      if (manualSupplementalText) {
        await api.uploadText(updated.session_id, manualSupplementalText);
        supplementalSaved = true;
      }
      if (supplementalSaved) {
        updated = await api.getSession(updated.session_id);
      }

      const canContinue = Boolean(updated.employee_profile);
      setSession(updated);
      setEmployeeResults([]);
      setProfileTextState(DEFAULT_PROFILE_TEXT);
      setSelectedFileState(null);
      setSelectedFileMetadataState(null);
      clearWorkflowDraftFields(['selectedEmployee', 'profileText', 'selectedFile']);
      const selectedEmployeeId = String(selectedProfile?.employee_id || '').trim() || null;
      if (profileChanged && preparedEmployeeIdRef.current !== selectedEmployeeId) {
        resetEmployeeDependentState();
      }
      preparedEmployeeIdRef.current = null;

      if (!canContinue) {
        showToast(supplementalSaved ? '额外提供的信息已保存到当前会话。' : '请先从员工库匹配并选择员工。');
        return false;
      }

      showToast('员工信息已确认');
      return true;
    });
  }, [beginFreshSessionFromSnapshot, clearWorkflowDraftFields, ensureSession, profileText, resetEmployeeDependentState, runTask, selectedEmployee, selectedFile, selectedFileMetadata, showToast]);

  const generateIntentPerformanceDraft = useCallback(async (
    intentId: string,
  ): Promise<IntentPerformanceDraftResponse> => {
    const { session: activeSession, token } = await beginLocaleOperation();
    const draft = await api.generateIntentPerformanceDraft(
      activeSession.session_id,
      intentId,
    );
    assertLocaleOperationCurrent(token, draft);
    return draft;
  }, [assertLocaleOperationCurrent, beginLocaleOperation]);

  const streamIntentPerformanceDraft = useCallback(async (
    intentId: string,
    onEvent: (event: string, data: Record<string, unknown>) => void,
    signal?: AbortSignal,
  ): Promise<void> => {
    const { session: activeSession, token } = await beginLocaleOperation();
    await api.streamIntentPerformanceDraft(
      activeSession.session_id,
      intentId,
      (event, data) => {
        if (!isLocaleOperationCurrent(token)) return;
        if (event === 'complete') assertLocaleOperationCurrent(token, data);
        onEvent(event, data);
      },
      signal,
    );
    assertLocaleOperationCurrent(token);
  }, [assertLocaleOperationCurrent, beginLocaleOperation, isLocaleOperationCurrent]);

  const confirmIntent = useCallback(async (
    performanceContext: string,
    performanceItems: IntentGoalPerformanceItem[],
  ) => {
    await runTask('确认意图', async () => {
      const activeSession = await ensureSession();
      const intentId = selectedIntentId || sessionIntentId(activeSession);
      if (!intentId) throw new Error('请选择对话意图。');
      if (!performanceContext.trim()) throw new Error('请填写并确认员工当前表现。');

      const currentIntentId = sessionIntentId(activeSession);
      const intentChanged = Boolean(currentIntentId && currentIntentId !== intentId);
      const performanceContextChanged = (
        sessionIntentPerformanceContext(activeSession) !== performanceContext
      );
      const setupChanged = intentChanged || performanceContextChanged;
      const needsFreshSession = setupChanged && hasDownstreamProgress(activeSession);
      const updated = needsFreshSession
        ? await beginFreshSessionFromSnapshot(buildSetupSnapshot(activeSession, {
          intentId,
          intentPerformanceContext: performanceContext,
          intentPerformanceItems: performanceItems,
        }))
        : await api.confirmIntent(
          activeSession.session_id,
          intentId,
          performanceItems,
        );

      setSession(updated);
      setSelectedIntentIdState(null);
      intentPerformanceDraftsRef.current = {};
      setIntentPerformanceDraftsState({});
      clearWorkflowDraftFields(['selectedIntentId', 'intentPerformanceDrafts']);
      if (setupChanged) clearDownstreamState();
      showToast('意图已确认');
    });
  }, [beginFreshSessionFromSnapshot, buildSetupSnapshot, clearDownstreamState, clearWorkflowDraftFields, ensureSession, runTask, selectedIntentId, showToast]);

  const confirmSimulation = useCallback(async () => {
    await runTask('确认人格与诉求', async () => {
      const activeSession = await ensureSession();
      const personality = normalizeBigFive(selectedPersonality);
      const primaryMotiveId = motiveSelection.hydrated
        ? selectedPrimaryMotiveId
        : sessionPrimaryMotiveId(activeSession);
      const selectedSecondaryIds = motiveSelection.hydrated
        ? selectedSecondaryMotiveIds
        : sessionSecondaryMotiveIds(activeSession);
      const secondaryMotiveIds = normalizeSecondaryMotiveIds(selectedSecondaryIds, primaryMotiveId);
      if (!primaryMotiveId) throw new Error('请选择主诉求。');
      if (!validSecondaryMotiveCount(secondaryMotiveIds)) throw new Error('辅诉求最多选择两个。');
      if (secondaryMotiveIds.includes(primaryMotiveId)) throw new Error('主诉求和辅诉求不能重复。');
      if (new Set(secondaryMotiveIds).size !== secondaryMotiveIds.length) throw new Error('辅诉求不能重复。');

      const personalityChanged = Boolean(activeSession.personality && !samePersonality(activeSession.personality, personality));
      const motiveChanged = Boolean(activeSession.motivation && (
        activeSession.motivation.primary_motive_id !== primaryMotiveId ||
        JSON.stringify(activeSession.motivation.secondary_motive_ids || []) !== JSON.stringify(secondaryMotiveIds)
      ));
      const needsFreshSession = (personalityChanged || motiveChanged) && hasDownstreamProgress(activeSession);

      const completed = needsFreshSession
        ? await beginFreshSessionFromSnapshot(buildSetupSnapshot(activeSession, { personality, primaryMotiveId, secondaryMotiveIds }))
        : await api.confirmSimulation(activeSession.session_id, personality, primaryMotiveId, secondaryMotiveIds).then(() => api.completeSetup(activeSession.session_id));

      setSession(completed);
      syncSimulationSelection(completed);
      clearWorkflowDraftFields([
        'selectedPersonality',
        'selectedPrimaryMotiveId',
        'selectedSecondaryMotiveIds',
      ]);
      if (personalityChanged || motiveChanged) clearDownstreamState();
      showToast('人格与诉求已确认');
    });
  }, [beginFreshSessionFromSnapshot, buildSetupSnapshot, clearDownstreamState, clearWorkflowDraftFields, ensureSession, motiveSelection.hydrated, runTask, selectedPersonality, selectedPrimaryMotiveId, selectedSecondaryMotiveIds, showToast, syncSimulationSelection]);

  const ensureGuidance = useCallback(async () => {
    const requestedEpoch = localeCoordinator.currentEpoch();
    if (
      (guidanceStatus === 'ready' && guidanceReport?.locale === languageRef.current)
      || (guidanceStatus === 'streaming' && guidanceInFlight.current === requestedEpoch)
      || guidanceInFlight.current === requestedEpoch
    ) return;
    guidanceInFlight.current = requestedEpoch;
    // Enter the generation state before session hydration so the first Guidance
    // frame never renders stale "idle" sections while the request is starting.
    setGuidanceReport(null);
    setGuidanceSections(createGuidanceSections('generating'));
    setGuidanceStatus('streaming');
    let operation: LocaleOperation;
    try {
      operation = await beginLocaleOperation();
    } catch (error) {
      setGuidanceSections(createGuidanceSections());
      setGuidanceStatus('idle');
      if (guidanceInFlight.current === requestedEpoch) guidanceInFlight.current = null;
      throw error;
    }
    const { session: activeSession, token } = operation;
    guidanceInFlight.current = token.epoch;
    if (!activeSession.setup_ready) {
      setGuidanceSections(createGuidanceSections());
      setGuidanceStatus('idle');
      if (guidanceInFlight.current === token.epoch) guidanceInFlight.current = null;
      showToast('请先完成员工信息、意图、人格与诉求设置。', 'error');
      return;
    }
    const performanceLocale = activeSession.intent?.performance_locale || 'zh-CN';
    if (activeSession.intent && performanceLocale !== token.locale) {
      setGuidanceSections(createGuidanceSections());
      setGuidanceStatus('idle');
      if (guidanceInFlight.current === token.epoch) guidanceInFlight.current = null;
      showToast('语言已切换，请先返回沟通意图页确认新语言的员工表现。', 'error');
      return;
    }
    if (activeSession.guidance_report_id) {
      try {
        const report = await api.getGuidance(activeSession.session_id);
        assertLocaleOperationCurrent(token, report);
        setGuidanceReport(report);
        setGuidanceSections(guidanceSectionsFromReport(report));
        setGuidanceStatus('ready');
        if (guidanceInFlight.current === token.epoch) guidanceInFlight.current = null;
        return;
      } catch (error) {
        if (!isLocaleOperationCurrent(token)) return;
        if (!shouldInvalidateStoredSession(error)) {
          setGuidanceStatus('partial_error');
          showToast(userFacingErrorMessage(error, undefined, '谈前指导载入失败，请稍后重试。'), 'error');
          if (guidanceInFlight.current === token.epoch) guidanceInFlight.current = null;
          return;
        }
      }
    }
    const textByKey = Object.fromEntries(guidanceSectionSeeds.map((section) => [section.key, ''])) as Record<GuidanceSectionKey, string>;
    let sectionFrame: number | null = null;
    let pendingSectionPatches = new Map<GuidanceSectionKey, Partial<GuidanceSectionDraft>>();
    const flushSectionPatches = () => {
      if (sectionFrame !== null) {
        window.cancelAnimationFrame(sectionFrame);
        sectionFrame = null;
      }
      if (!isLocaleOperationCurrent(token)) {
        pendingSectionPatches.clear();
        return;
      }
      if (!pendingSectionPatches.size) return;
      const patches = pendingSectionPatches;
      pendingSectionPatches = new Map();
      setGuidanceSections((current) => current.map((section) => {
        const patch = patches.get(section.key);
        return patch ? { ...section, ...patch } : section;
      }));
    };
    const patchSection = (
      key: GuidanceSectionKey,
      patch: Partial<GuidanceSectionDraft>,
      immediate = false,
    ) => {
      pendingSectionPatches.set(key, {
        ...pendingSectionPatches.get(key),
        ...patch,
      });
      if (immediate) {
        flushSectionPatches();
        return;
      }
      if (sectionFrame !== null) return;
      sectionFrame = window.requestAnimationFrame(() => {
        sectionFrame = null;
        flushSectionPatches();
      });
    };

    try {
      let completed = false;
      try {
        await api.streamGuidance(activeSession.session_id, (event, data) => {
          if (!isLocaleOperationCurrent(token)) return;
          const key = asGuidanceSectionKey(data.key);
          if (event === 'section_start' && key) {
            patchSection(key, { text: '', items: null, point_groups: null, status: 'generating', error: null }, true);
          }
          if (event === 'delta' && key) {
            textByKey[key] += String(data.text || '');
            patchSection(key, { text: textByKey[key], items: null, point_groups: null, status: 'generating', error: null });
          }
          if (event === 'section_done' && key) {
            const value = data.value;
            const pointGroups = data.point_groups;
            if (isGuidancePointGroups(pointGroups)) {
              patchSection(key, { text: '', items: null, point_groups: pointGroups, status: 'done', error: null }, true);
            } else if (Array.isArray(value)) {
              patchSection(key, { text: '', items: value as string[], point_groups: null, status: 'done', error: null }, true);
            } else if (typeof value === 'string') {
              textByKey[key] = value;
              patchSection(key, { text: value, items: null, point_groups: null, status: 'done', error: null }, true);
            } else {
              patchSection(key, { status: 'done', error: null }, true);
            }
          }
          if (event === 'section_error' && key) {
            patchSection(key, {
              status: 'error',
              error: userFacingErrorMessage(data.message, undefined, '该部分生成失败。'),
            }, true);
          }
          if (event === 'done') {
            completed = true;
            if (data.complete === false) {
              flushSectionPatches();
              setGuidanceStatus('partial_error');
              showToast('谈前指导部分生成失败，请重新生成。', 'error');
              return;
            }
            const report = data.report as GuidanceReport | undefined;
            if (!report) throw new Error('谈前指导流结束，但缺少最终结构化报告。');
            assertLocaleOperationCurrent(token, report);
            if (sectionFrame !== null) window.cancelAnimationFrame(sectionFrame);
            sectionFrame = null;
            pendingSectionPatches.clear();
            const finalSections = guidanceSectionsFromReport(report);
            const updatedState = (data.state as SessionState) || activeSession;
            assertLocaleOperationCurrent(token, updatedState);
            sessionRef.current = updatedState;
            setSession(updatedState);
            setGuidanceReport(report);
            setGuidanceSections(finalSections);
            setGuidanceStatus('ready');
          }
        });
      } catch (streamError) {
        if (!isLocaleOperationCurrent(token)) return;
        if (streamError instanceof StaleLocaleResultError) throw streamError;
        flushSectionPatches();
        const report = await api.generateGuidance(activeSession.session_id);
        assertLocaleOperationCurrent(token, report);
        const finalSections = guidanceSectionsFromReport(report);
        setGuidanceReport(report);
        setGuidanceSections(finalSections);
        const refreshed = await api.getSession(activeSession.session_id);
        assertLocaleOperationCurrent(token, refreshed);
        sessionRef.current = refreshed;
        setSession(refreshed);
        setGuidanceStatus('ready');
        showToast('流式生成中断，已使用普通模式完成谈前指导。');
        return;
      }
      if (!isLocaleOperationCurrent(token)) return;
      if (!completed) throw new Error('谈前指导生成中断，请重新生成。');
    } catch (error) {
      if (!isLocaleOperationCurrent(token)) return;
      flushSectionPatches();
      setGuidanceStatus('partial_error');
      showToast(userFacingErrorMessage(error, undefined, '谈前指导生成失败，请重新生成。'), 'error');
    } finally {
      if (sectionFrame !== null) window.cancelAnimationFrame(sectionFrame);
      if (guidanceInFlight.current === token.epoch) guidanceInFlight.current = null;
    }
  }, [assertLocaleOperationCurrent, beginLocaleOperation, guidanceReport?.locale, guidanceStatus, isLocaleOperationCurrent, localeCoordinator, showToast]);

  const updateRehearsalContext = useCallback(async (payload: RehearsalContextUpdatePayload): Promise<SessionState | null> => {
    if (rehearsalStreaming) {
      showToast('员工正在回复中，请等本轮回复结束后再调整模拟设定。', 'error');
      return null;
    }
    return await runTask('更新模拟设定', async () => {
      const activeSession = await ensureSession();
      const updated = await api.updateRehearsalContext(activeSession.session_id, payload);
      setSession(updated);
      setLiveConversation(null);
      setCoachReport(null);
      setReportStatus('idle');
      setCoachTasks(createCoachTasks());
      rehearsalRuntimeNoteRef.current = '';
      setRehearsalRuntimeNoteState('');
      clearWorkflowDraftFields(['runtimeNote']);
      const replacementNotes = Array.isArray(payload.runtime_notes)
        ? payload.runtime_notes
        : typeof payload.runtime_notes === 'string'
          ? [payload.runtime_notes]
          : [];
      const clearedOnly = payload.clear_context
        && !payload.runtime_note?.trim()
        && !replacementNotes.some((note) => note.trim());
      showToast(clearedOnly ? '会话背景已清空' : '会话背景已更新');
      return updated;
    });
  }, [clearWorkflowDraftFields, ensureSession, rehearsalStreaming, runTask, showToast]);

  const sendMessage = useCallback(async (
    message: string,
    speech?: RehearsalSpeechRequest,
    onSpeechFailure?: (event: string, data: Record<string, unknown>) => void,
    requestId?: string,
  ) => {
    const text = message.trim();
    if (!text) return false;
    if (rehearsalMessageInFlight.current) {
      showToast('上一轮员工回复尚未完成，本条消息已保留在输入队列中。', 'error');
      return false;
    }

    rehearsalMessageInFlight.current = true;
    const activeRequestId = requestId?.trim() || createRehearsalRequestId();
    const submittedAt = monotonicNowMs();
    let streamStartedAt: number | null = null;
    let firstVisibleAt: number | null = null;
    let lastVisibleAt: number | null = null;
    let previousConversation: ConversationTurn[] | null = null;
    let activeSession: SessionState | null = null;
    let operationToken: SessionLocaleOperationToken | null = null;
    let cancelScheduledDraftUpdate: () => void = () => {};
    let flushScheduledDraftUpdate: () => void = () => {};
    setRehearsalStreaming(true);
    try {
      const operation = await beginLocaleOperation();
      activeSession = operation.session;
      const token = operation.token;
      operationToken = token;
      const performanceLocale = activeSession.intent?.performance_locale || 'zh-CN';
      if (activeSession.intent && performanceLocale !== token.locale) {
        showToast('语言已切换，请先返回沟通意图页确认新语言的员工表现。', 'error');
        return false;
      }
      const base = activeSession.conversation || [];
      previousConversation = base;
      const streamDraft = createRehearsalStreamDraft(base, text);
      setLiveConversation(streamDraft.getSnapshot());
      let draftTimer: number | null = null;
      let draftPending = false;
      cancelScheduledDraftUpdate = () => {
        if (draftTimer !== null) window.clearTimeout(draftTimer);
        draftTimer = null;
        draftPending = false;
      };
      flushScheduledDraftUpdate = () => {
        if (draftTimer !== null) window.clearTimeout(draftTimer);
        draftTimer = null;
        if (!draftPending) return;
        draftPending = false;
        setLiveConversation(streamDraft.getSnapshot());
      };
      const scheduleDraftUpdate = () => {
        draftPending = true;
        if (draftTimer !== null) return;
        draftTimer = window.setTimeout(() => {
          draftTimer = null;
          flushScheduledDraftUpdate();
        }, REHEARSAL_STREAM_RENDER_INTERVAL_MS);
      };

      await api.streamRehearsalMessage(activeSession.session_id, text, (event, data) => {
        if (!isLocaleOperationCurrent(token)) return;
        if (event === 'start' && streamStartedAt === null) {
          streamStartedAt = monotonicNowMs();
        }
        if (event === 'delta') {
          const receivedAt = monotonicNowMs();
          if (firstVisibleAt === null) firstVisibleAt = receivedAt;
          lastVisibleAt = receivedAt;
          if (streamDraft.append(String(data.text || ''))) scheduleDraftUpdate();
        }
        if (event === 'done') {
          cancelScheduledDraftUpdate();
          const completedAt = monotonicNowMs();
          const serverTiming = asRehearsalTurnTiming(data.timing);
          const clientTiming: RehearsalClientTiming = {
            ...(streamStartedAt === null
              ? {}
              : { submit_to_start_ms: elapsedClientMs(submittedAt, streamStartedAt) }),
            ...(firstVisibleAt === null
              ? {}
              : {
                submit_to_first_visible_reply_ms: elapsedClientMs(
                  submittedAt,
                  firstVisibleAt,
                ),
              }),
            ...(firstVisibleAt === null || lastVisibleAt === null
              ? {}
              : {
                visible_reply_stream_ms: elapsedClientMs(
                  firstVisibleAt,
                  lastVisibleAt,
                ),
              }),
            submit_to_done_ms: elapsedClientMs(submittedAt, completedAt),
            recovered: false,
            transport: 'stream',
          };
          const updated = attachLatestEmployeeTiming(
            (data.state as SessionState) || activeSession,
            clientTiming,
            serverTiming,
          );
          assertLocaleOperationCurrent(token, updated);
          sessionRef.current = updated;
          setSession(updated);
          setLiveConversation(null);
        }
      }, speech, activeRequestId);
      if (!isLocaleOperationCurrent(token)) return false;
      flushScheduledDraftUpdate();
      return true;
    } catch (error) {
      cancelScheduledDraftUpdate();
      if (error instanceof StaleLocaleResultError) {
        setLiveConversation(previousConversation);
        showToast(error.message, 'error');
        return false;
      }
      let finalError: unknown = error;
      try {
        if (activeSession && operationToken) {
          const token = operationToken;
          if (!isLocaleOperationCurrent(token)) return false;
          const refreshed = await api.getSession(activeSession.session_id);
          assertLocaleOperationCurrent(token, refreshed);
          if (hasCompletedRehearsalRequest(
            refreshed.conversation || [],
            activeRequestId,
          )) {
            const completedAt = monotonicNowMs();
            const recovered = attachLatestEmployeeTiming(
              refreshed,
              {
                ...(streamStartedAt === null
                  ? {}
                  : {
                    submit_to_start_ms: elapsedClientMs(
                      submittedAt,
                      streamStartedAt,
                    ),
                  }),
                ...(firstVisibleAt === null
                  ? {}
                  : {
                    submit_to_first_visible_reply_ms: elapsedClientMs(
                      submittedAt,
                      firstVisibleAt,
                    ),
                  }),
                ...(firstVisibleAt === null || lastVisibleAt === null
                  ? {}
                  : {
                    visible_reply_stream_ms: elapsedClientMs(
                      firstVisibleAt,
                      lastVisibleAt,
                    ),
                  }),
                submit_to_done_ms: elapsedClientMs(submittedAt, completedAt),
                recovered: true,
                transport: 'refreshed_after_stream',
              },
            );
            assertLocaleOperationCurrent(token, recovered);
            sessionRef.current = recovered;
            setSession(recovered);
            setLiveConversation(null);
            return true;
          }

          const updated = await api.sendRehearsalMessage(
            activeSession.session_id,
            text,
            activeRequestId,
          );
          assertLocaleOperationCurrent(token, updated);
          const recovered = attachLatestEmployeeTiming(
            updated,
            {
              ...(streamStartedAt === null
                ? {}
                : {
                  submit_to_start_ms: elapsedClientMs(
                    submittedAt,
                    streamStartedAt,
                  ),
                }),
              ...(firstVisibleAt === null
                ? {}
                : {
                  submit_to_first_visible_reply_ms: elapsedClientMs(
                    submittedAt,
                    firstVisibleAt,
                  ),
                }),
              ...(firstVisibleAt === null || lastVisibleAt === null
                ? {}
                : {
                  visible_reply_stream_ms: elapsedClientMs(
                    firstVisibleAt,
                    lastVisibleAt,
                  ),
                }),
              submit_to_done_ms: elapsedClientMs(
                submittedAt,
                monotonicNowMs(),
              ),
              recovered: true,
              transport: 'nonstream_fallback',
            },
          );
          sessionRef.current = recovered;
          setSession(recovered);
          setLiveConversation(null);
          onSpeechFailure?.('speech_error', {
            message: translate(
              '流式连接中断，本轮已切换为文字回复。',
              'Streaming was interrupted. This turn switched to a text response.',
            ),
            error_type: 'StreamingFallback',
            session_id: activeSession.session_id,
            speech_stream_id: speech?.stream_id,
          });
          showToast('流式预演中断，已使用普通模式完成本轮回复。');
          return true;
        }
      } catch (fallbackError) {
        finalError = fallbackError;
      }
      const err = finalError as Error & { name?: string };
      const errorMessage = err?.name === 'AbortError'
        ? '请求超时，请稍后重试。'
        : userFacingErrorMessage(finalError, undefined, '本轮员工回复生成失败，请重试。');
      setLiveConversation(previousConversation);
      onSpeechFailure?.('speech_error', {
        message: `${translate(
          '本轮语音未完成：',
          'Speech for this turn did not complete: ',
        )}${errorMessage}`,
        error_type: err?.name || 'RehearsalStreamError',
        session_id: activeSession?.session_id,
        speech_stream_id: speech?.stream_id,
      });
      showToast(errorMessage, 'error');
      return false;
    } finally {
      cancelScheduledDraftUpdate();
      rehearsalMessageInFlight.current = false;
      setRehearsalStreaming(false);
    }
  }, [assertLocaleOperationCurrent, beginLocaleOperation, isLocaleOperationCurrent, localeCoordinator, showToast, translate]);

  const endRehearsal = useCallback(async () => {
    await runTask('结束预演', async () => {
      const { session: activeSession, token } = await beginLocaleOperation();
      const updated = await api.endRehearsal(activeSession.session_id);
      assertLocaleOperationCurrent(token, updated);
      sessionRef.current = updated;
      setSession(updated);
      setLiveConversation(null);
    });
  }, [assertLocaleOperationCurrent, beginLocaleOperation, runTask]);

  const ensureReport = useCallback(async (force = false) => {
    const requestedEpoch = localeCoordinator.currentEpoch();
    if (
      (!force && ((coachReport?.locale === languageRef.current) || reportStatus === 'partial_error'))
      || (reportStatus === 'streaming' && reportInFlight.current === requestedEpoch)
      || reportInFlight.current === requestedEpoch
    ) return;
    reportInFlight.current = requestedEpoch;
    let operation: LocaleOperation;
    try {
      operation = await beginLocaleOperation();
    } catch (error) {
      if (reportInFlight.current === requestedEpoch) reportInFlight.current = null;
      throw error;
    }
    const { session: activeSession, token } = operation;
    reportInFlight.current = token.epoch;
    if (activeSession.coach_report_id && !force) {
      try {
        // POST keeps the read fast when the report version is current, while
        // allowing the backend to regenerate a stale prompt/model version.
        const report = await api.generateCoachReport(activeSession.session_id);
        assertLocaleOperationCurrent(token, report);
        setCoachReport(report);
        setCoachTasks(coachTasksFromReport(report));
        if (!isCompleteCoachReport(report)) {
          setReportStatus('partial_error');
          showToast('复盘报告部分生成失败，请重试。', 'error');
        } else {
          setReportStatus('ready');
        }
        if (reportInFlight.current === token.epoch) reportInFlight.current = null;
        return;
      } catch (error) {
        if (!isLocaleOperationCurrent(token)) return;
        if (!shouldInvalidateStoredSession(error)) {
          setReportStatus('partial_error');
          showToast(userFacingErrorMessage(error, undefined, '复盘报告载入失败，请稍后重试。'), 'error');
          if (reportInFlight.current === token.epoch) reportInFlight.current = null;
          return;
        }
      }
    }
    const managerTurns = (activeSession.conversation || []).filter((turn) => turn.speaker === 'manager');
    if (!managerTurns.length) {
      showToast('请先完成至少一轮多轮预演，再生成复盘报告。', 'error');
      if (reportInFlight.current === token.epoch) reportInFlight.current = null;
      return;
    }
    setCoachReport(null);
    setCoachTasks(createCoachTasks('running'));
    setReportStatus('streaming');
    const patchTask = (taskId: string, patch: Partial<CoachTaskDraft>) => {
      setCoachTasks((current) => current.map((task) => task.task_id === taskId ? { ...task, ...patch } : task));
    };

    try {
      let completed = false;
      try {
        await api.streamCoachReport(activeSession.session_id, (event, data) => {
          if (!isLocaleOperationCurrent(token)) return;
          const taskId = typeof data.task_id === 'string' ? data.task_id : '';
          if (event === 'task_start' && taskId) {
            patchTask(taskId, { task_name: String(data.task_name || taskId), status: 'running', error: null });
          }
          if (event === 'task_done' && taskId) {
            const result = (data.result || {}) as CoachTaskResult;
            patchTask(taskId, {
              task_name: String(data.task_name || result.task_name || taskId),
              status: 'done',
              result,
              summary: String(result.summary || ''),
              score: typeof result.score === 'number' ? result.score : null,
              error: null,
            });
          }
          if (event === 'task_error' && taskId) {
            const result = data.result ? data.result as CoachTaskResult : null;
            patchTask(taskId, {
              task_name: String(data.task_name || result?.task_name || taskId),
              status: 'error',
              result,
              summary: result?.summary || '',
              score: typeof result?.score === 'number' ? result.score : null,
              error: userFacingErrorMessage(data.message, undefined, '该评估任务失败。'),
            });
          }
          if (event === 'done') {
            completed = true;
            const report = (data.report as CoachReport) || null;
            if (!report) throw new Error('复盘报告流结束，但缺少最终结构化报告。');
            assertLocaleOperationCurrent(token, report);
            const updatedState = (data.state as SessionState) || activeSession;
            assertLocaleOperationCurrent(token, updatedState);
            sessionRef.current = updatedState;
            setSession(updatedState);
            setCoachReport(report);
            if (data.complete === false || !isCompleteCoachReport(report)) {
              setReportStatus('partial_error');
              showToast('复盘报告部分生成失败，请重试。', 'error');
              return;
            }
            setReportStatus('ready');
          }
        });
      } catch (streamError) {
        if (!isLocaleOperationCurrent(token)) return;
        if (streamError instanceof StaleLocaleResultError) throw streamError;
        const report = await api.generateCoachReport(activeSession.session_id);
        assertLocaleOperationCurrent(token, report);
        setCoachReport(report);
        setCoachTasks(coachTasksFromReport(report));
        const refreshed = await api.getSession(activeSession.session_id);
        assertLocaleOperationCurrent(token, refreshed);
        sessionRef.current = refreshed;
        setSession(refreshed);
        if (!isCompleteCoachReport(report)) {
          setReportStatus('partial_error');
          showToast('流式复盘中断，普通模式仍有部分维度失败，请重试。', 'error');
        } else {
          setReportStatus('ready');
          showToast('流式复盘中断，已使用普通模式完成报告。');
        }
        return;
      }
      if (!isLocaleOperationCurrent(token)) return;
      if (!completed) throw new Error('复盘报告生成中断，请重新生成。');
    } catch (error) {
      if (!isLocaleOperationCurrent(token)) return;
      setReportStatus('partial_error');
      showToast(userFacingErrorMessage(error, undefined, '复盘报告生成失败，请重新生成。'), 'error');
    } finally {
      if (reportInFlight.current === token.epoch) reportInFlight.current = null;
    }
  }, [assertLocaleOperationCurrent, beginLocaleOperation, coachReport?.locale, isLocaleOperationCurrent, localeCoordinator, reportStatus, showToast]);

  const exportReport = useCallback(() => window.print(), []);
  const localizedGuidanceSections = useMemo(() => guidanceSections.map((section) => ({
    ...section,
    title: translate(section.title),
    error: section.error ? translate(section.error) : section.error,
  })), [guidanceSections, translate]);
  const localizedCoachTasks = useMemo(() => coachTasks.map((task) => ({
    ...task,
    task_name: translate(task.task_name),
    error: task.error ? translate(task.error) : task.error,
  })), [coachTasks, translate]);
  const feedbackValue = useMemo<WorkflowFeedbackContextValue>(() => ({
    loading: { ...loading, text: translate(loading.text) },
    toast: toast ? { ...toast, message: translate(toast.message) } : null,
  }), [loading, toast, translate]);

  const value = useMemo<WorkflowContextValue>(() => ({
    sessionId,
    session,
    options,
    profileText,
    selectedFile,
    selectedFileMetadata,
    employeeResults,
    selectedEmployee,
    selectedIntentId: effectiveSelectedIntentId,
    intentPerformanceDrafts,
    selectedPersonality: effectiveSelectedPersonality,
    selectedPrimaryMotiveId: effectiveSelectedPrimaryMotiveId,
    selectedSecondaryMotiveIds: effectiveSelectedSecondaryMotiveIds,
    guidanceReport,
    guidanceSections: localizedGuidanceSections,
    guidanceStatus,
    coachReport,
    reportStatus,
    coachTasks: localizedCoachTasks,
    liveConversation,
    rehearsalStreaming,
    rehearsalDraft,
    rehearsalRuntimeNote,
    displayedProfile,
    setProfileText,
    setSelectedFile,
    discardSelectedFileMetadata,
    setSelectedIntentId,
    setIntentPerformanceDrafts,
    updatePersonalityDimension,
    setSelectedPrimaryMotiveId,
    setSelectedSecondaryMotiveIds,
    setRehearsalDraft,
    setRehearsalRuntimeNote,
    bootstrap,
    lookupEmployee,
    selectEmployee,
    confirmProfile,
    generateIntentPerformanceDraft,
    streamIntentPerformanceDraft,
    confirmIntent,
    confirmSimulation,
    ensureGuidance,
    startNewSession,
    ensureSessionLocale,
    updateRehearsalContext,
    sendMessage,
    endRehearsal,
    ensureReport,
    exportReport,
    showToast,
  }), [
    sessionId,
    session,
    options,
    profileText,
    selectedFile,
    selectedFileMetadata,
    employeeResults,
    selectedEmployee,
    effectiveSelectedIntentId,
    intentPerformanceDrafts,
    effectiveSelectedPersonality,
    effectiveSelectedPrimaryMotiveId,
    effectiveSelectedSecondaryMotiveIds,
    guidanceReport,
    localizedGuidanceSections,
    guidanceStatus,
    coachReport,
    reportStatus,
    localizedCoachTasks,
    liveConversation,
    rehearsalStreaming,
    rehearsalDraft,
    rehearsalRuntimeNote,
    displayedProfile,
    setProfileText,
    setSelectedFile,
    discardSelectedFileMetadata,
    setSelectedIntentId,
    setIntentPerformanceDrafts,
    updatePersonalityDimension,
    setSelectedPrimaryMotiveId,
    setSelectedSecondaryMotiveIds,
    setRehearsalDraft,
    setRehearsalRuntimeNote,
    bootstrap,
    lookupEmployee,
    selectEmployee,
    confirmProfile,
    generateIntentPerformanceDraft,
    streamIntentPerformanceDraft,
    confirmIntent,
    confirmSimulation,
    ensureGuidance,
    startNewSession,
    ensureSessionLocale,
    updateRehearsalContext,
    sendMessage,
    endRehearsal,
    ensureReport,
    exportReport,
    showToast,
  ]);
  const navigationValue = useMemo<WorkflowNavigationContextValue>(() => ({
    session,
    hasIntentOptions: options.intents.length > 0,
    selectedIntentId: effectiveSelectedIntentId,
    guidanceStatus,
    reportStatus,
    bootstrapStatus,
    bootstrap,
    ensureGuidance,
    ensureReport,
  }), [
    bootstrap,
    bootstrapStatus,
    effectiveSelectedIntentId,
    ensureGuidance,
    ensureReport,
    guidanceStatus,
    options.intents.length,
    reportStatus,
    session,
  ]);

  return (
    <WorkflowFeedbackContext.Provider value={feedbackValue}>
      <WorkflowNavigationContext.Provider value={navigationValue}>
        <WorkflowContext.Provider value={value}>{children}</WorkflowContext.Provider>
      </WorkflowNavigationContext.Provider>
    </WorkflowFeedbackContext.Provider>
  );
}

export function useWorkflow() {
  const context = useContext(WorkflowContext);
  if (!context) throw new Error('useWorkflow must be used inside WorkflowProvider');
  return context;
}

export function useWorkflowFeedback() {
  const context = useContext(WorkflowFeedbackContext);
  if (!context) throw new Error('useWorkflowFeedback must be used inside WorkflowProvider');
  return context;
}

export function useWorkflowNavigation() {
  const context = useContext(WorkflowNavigationContext);
  if (!context) throw new Error('useWorkflowNavigation must be used inside WorkflowProvider');
  return context;
}

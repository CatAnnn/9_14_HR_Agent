import type { StepKey } from '../types/domain';

export const WORKFLOW_GUIDE_VERSION = 3;

export type WorkflowGuidePageKey = StepKey | 'intent-performance';

export interface WorkflowGuideStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

export interface WorkflowGuidePageState {
  dismissed: boolean;
}

export interface WorkflowGuideState {
  version: number;
  pages: Partial<Record<WorkflowGuidePageKey, WorkflowGuidePageState>>;
}

const GUIDE_PAGE_KEYS = new Set<WorkflowGuidePageKey>([
  'profile',
  'intent',
  'intent-performance',
  'simulation',
  'guidance',
  'rehearsal',
  'report',
]);

export function emptyWorkflowGuideState(): WorkflowGuideState {
  return { version: WORKFLOW_GUIDE_VERSION, pages: {} };
}

export function workflowGuideStorageKey(userKey: string): string {
  return `hr-agent:workflow-guide:v${WORKFLOW_GUIDE_VERSION}:${encodeURIComponent(userKey.trim().toLowerCase())}`;
}

function validPageState(value: unknown): value is WorkflowGuidePageState {
  if (!value || typeof value !== 'object') return false;
  const page = value as Partial<WorkflowGuidePageState>;
  return typeof page.dismissed === 'boolean';
}

export function readWorkflowGuideState(
  storage: WorkflowGuideStorage,
  userKey: string,
): WorkflowGuideState {
  try {
    const raw = storage.getItem(workflowGuideStorageKey(userKey));
    if (!raw) return emptyWorkflowGuideState();
    const parsed = JSON.parse(raw) as Partial<WorkflowGuideState>;
    if (parsed.version !== WORKFLOW_GUIDE_VERSION || !parsed.pages || typeof parsed.pages !== 'object') {
      return emptyWorkflowGuideState();
    }
    const pages: WorkflowGuideState['pages'] = {};
    Object.entries(parsed.pages).forEach(([key, value]) => {
      if (GUIDE_PAGE_KEYS.has(key as WorkflowGuidePageKey) && validPageState(value)) {
        pages[key as WorkflowGuidePageKey] = {
          dismissed: value.dismissed,
        };
      }
    });
    return { version: WORKFLOW_GUIDE_VERSION, pages };
  } catch {
    return emptyWorkflowGuideState();
  }
}

export function writeWorkflowGuideState(
  storage: WorkflowGuideStorage,
  userKey: string,
  state: WorkflowGuideState,
): boolean {
  try {
    storage.setItem(workflowGuideStorageKey(userKey), JSON.stringify(state));
    return true;
  } catch {
    return false;
  }
}

export function dismissWorkflowGuidePage(
  state: WorkflowGuideState,
  page: WorkflowGuidePageKey,
): WorkflowGuideState {
  return {
    ...state,
    pages: { ...state.pages, [page]: { dismissed: true } },
  };
}

export function resetWorkflowGuidePage(
  state: WorkflowGuideState,
  page: WorkflowGuidePageKey,
): WorkflowGuideState {
  const pages = { ...state.pages };
  delete pages[page];
  return { ...state, pages };
}

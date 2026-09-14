import type {
  BigFivePersonality,
  EmployeeRecord,
  IntentPerformanceDraftResponse,
} from '../types/domain';
import { SUPPORTED_LANGUAGES } from '../i18n/languageRuntime.ts';

export const WORKFLOW_DRAFT_STORAGE_VERSION = 1 as const;

const WORKFLOW_DRAFT_STORAGE_NAMESPACE = 'workflow-draft';
const LEGACY_PROFILE_TEXT_DRAFT_SUFFIX = 'profile_text_draft';

export interface WorkflowDraftFileMetadata {
  name: string;
  size: number;
  type: string;
  lastModified: number;
}

/**
 * Only fields touched by the user need to be present. Explicit `null` values are
 * retained so callers can distinguish clearing a selection from an untouched
 * field.
 */
export interface WorkflowDraft {
  selectedEmployee?: EmployeeRecord | null;
  profileText?: string;
  selectedIntentId?: string | null;
  intentPerformanceDrafts?: Record<string, IntentPerformanceDraftResponse>;
  selectedPersonality?: BigFivePersonality | null;
  selectedPrimaryMotiveId?: string | null;
  selectedSecondaryMotiveIds?: string[];
  rehearsalDraft?: string;
  runtimeNote?: string;
  selectedFile?: WorkflowDraftFileMetadata | null;
}

export interface StoredWorkflowDraftV1 {
  version: typeof WORKFLOW_DRAFT_STORAGE_VERSION;
  draft: WorkflowDraft;
}

/** The subset of the browser Storage API used by this module. */
export interface WorkflowDraftStorage {
  readonly length: number;
  key(index: number): string | null;
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

type UnknownRecord = Record<string, unknown>;

function isRecord(value: unknown): value is UnknownRecord {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function hasOwn(record: UnknownRecord, key: string): boolean {
  return Object.prototype.hasOwnProperty.call(record, key);
}

function validOptional(
  record: UnknownRecord,
  key: string,
  predicate: (value: unknown) => boolean,
): boolean {
  return !hasOwn(record, key) || predicate(record[key]);
}

function isNullableString(value: unknown): boolean {
  return value === null || typeof value === 'string';
}

function isStringArray(value: unknown): boolean {
  return Array.isArray(value) && value.every((item) => typeof item === 'string');
}

function isBigFivePersonality(value: unknown): value is BigFivePersonality {
  if (!isRecord(value)) return false;
  return [
    value.openness,
    value.conscientiousness,
    value.extraversion,
    value.agreeableness,
    value.neuroticism,
  ].every((score) => typeof score === 'number' && Number.isFinite(score));
}

function isIntentPerformanceDraft(value: unknown): value is IntentPerformanceDraftResponse {
  if (!isRecord(value)) return false;
  if (
    typeof value.intent_id !== 'string'
    || !SUPPORTED_LANGUAGES.includes(value.locale as (typeof SUPPORTED_LANGUAGES)[number])
    || typeof value.performance_context !== 'string'
  ) {
    return false;
  }
  if (!Array.isArray(value.performance_items)) return false;
  return value.performance_items.every((item) => (
    isRecord(item)
    && typeof item.goal === 'string'
    && typeof item.current_performance === 'string'
    && validOptional(item, 'generation_reason', isNullableString)
  ));
}

function isIntentPerformanceDraftMap(value: unknown): value is Record<string, IntentPerformanceDraftResponse> {
  return isRecord(value) && Object.values(value).every(isIntentPerformanceDraft);
}

function isFileMetadata(value: unknown): value is WorkflowDraftFileMetadata {
  if (!isRecord(value)) return false;
  return (
    typeof value.name === 'string'
    && typeof value.type === 'string'
    && typeof value.size === 'number'
    && Number.isFinite(value.size)
    && value.size >= 0
    && typeof value.lastModified === 'number'
    && Number.isFinite(value.lastModified)
    && value.lastModified >= 0
  );
}

function isWorkflowDraft(value: unknown): value is WorkflowDraft {
  if (!isRecord(value)) return false;
  return (
    validOptional(value, 'selectedEmployee', (item) => item === null || isRecord(item))
    && validOptional(value, 'profileText', (item) => typeof item === 'string')
    && validOptional(value, 'selectedIntentId', isNullableString)
    && validOptional(value, 'intentPerformanceDrafts', isIntentPerformanceDraftMap)
    && validOptional(value, 'selectedPersonality', (item) => item === null || isBigFivePersonality(item))
    && validOptional(value, 'selectedPrimaryMotiveId', isNullableString)
    && validOptional(value, 'selectedSecondaryMotiveIds', isStringArray)
    && validOptional(value, 'rehearsalDraft', (item) => typeof item === 'string')
    && validOptional(value, 'runtimeNote', (item) => typeof item === 'string')
    && validOptional(value, 'selectedFile', (item) => item === null || isFileMetadata(item))
  );
}

function parseStoredWorkflowDraft(raw: string): WorkflowDraft | null {
  try {
    const value: unknown = JSON.parse(raw);
    if (!isRecord(value) || value.version !== WORKFLOW_DRAFT_STORAGE_VERSION) return null;
    return isWorkflowDraft(value.draft) ? value.draft : null;
  } catch {
    return null;
  }
}

function serializeWorkflowDraft(draft: WorkflowDraft): string | null {
  if (!isWorkflowDraft(draft)) return null;
  try {
    const stored: StoredWorkflowDraftV1 = {
      version: WORKFLOW_DRAFT_STORAGE_VERSION,
      draft,
    };
    return JSON.stringify(stored);
  } catch {
    return null;
  }
}

export function workflowDraftStoragePrefix(sessionStorageKey: string): string {
  return `${sessionStorageKey}:${WORKFLOW_DRAFT_STORAGE_NAMESPACE}:v${WORKFLOW_DRAFT_STORAGE_VERSION}:`;
}

export function workflowDraftStorageKey(
  sessionStorageKey: string,
  sessionId: string | null | undefined,
): string {
  return `${workflowDraftStoragePrefix(sessionStorageKey)}${sessionId || 'pending'}`;
}

export function legacyProfileDraftStorageKey(
  sessionStorageKey: string,
  sessionId: string | null | undefined,
): string {
  return `${sessionStorageKey}:${LEGACY_PROFILE_TEXT_DRAFT_SUFFIX}:${sessionId || 'pending'}`;
}

export function readWorkflowDraft(
  storage: WorkflowDraftStorage | null | undefined,
  sessionStorageKey: string,
  sessionId: string | null | undefined,
): WorkflowDraft | null {
  if (!storage) return null;
  try {
    const raw = storage.getItem(workflowDraftStorageKey(sessionStorageKey, sessionId));
    return raw === null ? null : parseStoredWorkflowDraft(raw);
  } catch {
    return null;
  }
}

export function writeWorkflowDraft(
  storage: WorkflowDraftStorage | null | undefined,
  sessionStorageKey: string,
  sessionId: string | null | undefined,
  draft: WorkflowDraft,
): boolean {
  if (!storage) return false;
  const serialized = serializeWorkflowDraft(draft);
  if (serialized === null) return false;
  try {
    storage.setItem(workflowDraftStorageKey(sessionStorageKey, sessionId), serialized);
    return true;
  } catch {
    return false;
  }
}

export function removeWorkflowDraft(
  storage: WorkflowDraftStorage | null | undefined,
  sessionStorageKey: string,
  sessionId: string | null | undefined,
): boolean {
  if (!storage) return false;
  try {
    storage.removeItem(workflowDraftStorageKey(sessionStorageKey, sessionId));
    return true;
  } catch {
    return false;
  }
}

export function moveWorkflowDraft(
  storage: WorkflowDraftStorage | null | undefined,
  sessionStorageKey: string,
  fromSessionId: string | null | undefined,
  toSessionId: string | null | undefined,
): boolean {
  if (!storage) return false;
  const fromKey = workflowDraftStorageKey(sessionStorageKey, fromSessionId);
  const toKey = workflowDraftStorageKey(sessionStorageKey, toSessionId);

  try {
    const raw = storage.getItem(fromKey);
    if (raw === null || parseStoredWorkflowDraft(raw) === null) return false;
    if (fromKey === toKey) return true;

    storage.setItem(toKey, raw);
    storage.removeItem(fromKey);
    return true;
  } catch {
    return false;
  }
}

function removeWorkflowDraftKeys(
  storage: WorkflowDraftStorage | null | undefined,
  matches: (key: string) => boolean,
): number {
  if (!storage) return 0;
  let length: number;
  try {
    length = storage.length;
  } catch {
    return 0;
  }

  let removed = 0;
  for (let index = length - 1; index >= 0; index -= 1) {
    try {
      const key = storage.key(index);
      if (key !== null && matches(key)) {
        storage.removeItem(key);
        removed += 1;
      }
    } catch {
      // A single inaccessible entry must not prevent the remaining cleanup.
    }
  }
  return removed;
}

/** Removes the current v1 draft and legacy profile draft for one exact session. */
export function removeWorkflowSessionDrafts(
  storage: WorkflowDraftStorage | null | undefined,
  sessionStorageKey: string,
  sessionId: string | null | undefined,
): number {
  const currentKey = workflowDraftStorageKey(sessionStorageKey, sessionId);
  const legacyKey = legacyProfileDraftStorageKey(sessionStorageKey, sessionId);
  return removeWorkflowDraftKeys(
    storage,
    (key) => key === currentKey || key === legacyKey,
  );
}

/** Removes every workflow-draft version and legacy profile draft in one namespace. */
export function clearAllWorkflowDrafts(
  storage: WorkflowDraftStorage | null | undefined,
  sessionStorageKey: string,
): number {
  const workflowPrefix = `${sessionStorageKey}:${WORKFLOW_DRAFT_STORAGE_NAMESPACE}:`;
  const legacyPrefix = `${sessionStorageKey}:${LEGACY_PROFILE_TEXT_DRAFT_SUFFIX}:`;
  return removeWorkflowDraftKeys(
    storage,
    (key) => key.startsWith(workflowPrefix) || key.startsWith(legacyPrefix),
  );
}

/** Removes all v1 workflow drafts belonging to one session-id storage key. */
export function clearWorkflowDrafts(
  storage: WorkflowDraftStorage | null | undefined,
  sessionStorageKey: string,
): number {
  if (!storage) return 0;
  const prefix = workflowDraftStoragePrefix(sessionStorageKey);
  let length: number;
  try {
    length = storage.length;
  } catch {
    return 0;
  }

  let removed = 0;
  for (let index = length - 1; index >= 0; index -= 1) {
    try {
      const key = storage.key(index);
      if (key?.startsWith(prefix)) {
        storage.removeItem(key);
        removed += 1;
      }
    } catch {
      // A single inaccessible entry must not prevent the remaining cleanup.
    }
  }
  return removed;
}

/** A stored session is stale only when the server says it is absent or gone. */
export function shouldInvalidateStoredSession(error: unknown): boolean {
  if (!isRecord(error)) return false;
  try {
    return error.status === 404 || error.status === 410;
  } catch {
    return false;
  }
}

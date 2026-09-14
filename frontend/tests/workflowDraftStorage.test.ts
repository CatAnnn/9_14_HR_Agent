import assert from 'node:assert/strict';
import test from 'node:test';

import {
  WORKFLOW_DRAFT_STORAGE_VERSION,
  clearAllWorkflowDrafts,
  clearWorkflowDrafts,
  legacyProfileDraftStorageKey,
  moveWorkflowDraft,
  readWorkflowDraft,
  removeWorkflowDraft,
  removeWorkflowSessionDrafts,
  shouldInvalidateStoredSession,
  workflowDraftStorageKey,
  workflowDraftStoragePrefix,
  writeWorkflowDraft,
  type WorkflowDraft,
  type WorkflowDraftStorage,
} from '../src/utils/workflowDraftStorage.ts';

class MemoryStorage implements WorkflowDraftStorage {
  protected readonly values = new Map<string, string>();

  get length(): number {
    return this.values.size;
  }

  key(index: number): string | null {
    return [...this.values.keys()][index] ?? null;
  }

  getItem(key: string): string | null {
    return this.values.get(key) ?? null;
  }

  setItem(key: string, value: string): void {
    this.values.set(key, value);
  }

  removeItem(key: string): void {
    this.values.delete(key);
  }
}

const fullDraft: WorkflowDraft = {
  selectedEmployee: {
    employee_id: 'e-7',
    name: 'Alex',
    profile_text: 'Existing profile',
  },
  profileText: 'Additional context',
  selectedIntentId: 'performance',
  intentPerformanceDrafts: {
    performance: {
      intent_id: 'performance',
      locale: 'en',
      performance_context: 'Discuss delivery expectations.',
      performance_items: [
        {
          goal: 'Ship the release',
          current_performance: 'Milestone is at risk.',
          generation_reason: 'Latest status',
        },
      ],
    },
  },
  selectedPersonality: {
    openness: 4,
    conscientiousness: 7,
    extraversion: 5,
    agreeableness: 6,
    neuroticism: 3,
  },
  selectedPrimaryMotiveId: 'achievement',
  selectedSecondaryMotiveIds: ['certainty', 'belonging'],
  rehearsalDraft: 'I would like to discuss the release.',
  runtimeNote: 'Keep the conversation concrete.',
  selectedFile: {
    name: 'profile.pdf',
    size: 2048,
    type: 'application/pdf',
    lastModified: 1_700_000_000_000,
  },
};

test('builds a namespaced v1 key for pending and active sessions', () => {
  assert.equal(workflowDraftStoragePrefix('hr_agent_session_id'), 'hr_agent_session_id:workflow-draft:v1:');
  assert.equal(
    workflowDraftStorageKey('hr_agent_session_id', null),
    'hr_agent_session_id:workflow-draft:v1:pending',
  );
  assert.equal(
    workflowDraftStorageKey('hr_agent_session_id', 'session-12'),
    'hr_agent_session_id:workflow-draft:v1:session-12',
  );
  assert.equal(
    legacyProfileDraftStorageKey('hr_agent_session_id', null),
    'hr_agent_session_id:profile_text_draft:pending',
  );
  assert.equal(
    legacyProfileDraftStorageKey('hr_agent_session_id', 'session-12'),
    'hr_agent_session_id:profile_text_draft:session-12',
  );
});

test('round-trips every optional workflow draft field in a versioned envelope', () => {
  const storage = new MemoryStorage();
  assert.equal(writeWorkflowDraft(storage, 'session-key', 'session-12', fullDraft), true);
  assert.deepEqual(readWorkflowDraft(storage, 'session-key', 'session-12'), fullDraft);

  const raw = storage.getItem(workflowDraftStorageKey('session-key', 'session-12'));
  assert.equal(JSON.parse(raw ?? '{}').version, WORKFLOW_DRAFT_STORAGE_VERSION);
});

test('treats malformed, incompatible, and structurally invalid entries as absent', () => {
  const storage = new MemoryStorage();
  const key = workflowDraftStorageKey('session-key', null);

  storage.setItem(key, '{not json');
  assert.equal(readWorkflowDraft(storage, 'session-key', null), null);

  storage.setItem(key, JSON.stringify({ version: 0, draft: fullDraft }));
  assert.equal(readWorkflowDraft(storage, 'session-key', null), null);

  storage.setItem(key, JSON.stringify({ version: 1, draft: { selectedSecondaryMotiveIds: 'certainty' } }));
  assert.equal(readWorkflowDraft(storage, 'session-key', null), null);
});

test('read, write, and remove degrade without throwing when storage fails', () => {
  const unavailable: WorkflowDraftStorage = {
    get length() {
      throw new Error('blocked');
    },
    key() {
      throw new Error('blocked');
    },
    getItem() {
      throw new Error('blocked');
    },
    setItem() {
      throw new Error('quota');
    },
    removeItem() {
      throw new Error('blocked');
    },
  };

  assert.equal(readWorkflowDraft(unavailable, 'session-key', null), null);
  assert.equal(writeWorkflowDraft(unavailable, 'session-key', null, fullDraft), false);
  assert.equal(removeWorkflowDraft(unavailable, 'session-key', null), false);
  assert.equal(clearWorkflowDrafts(unavailable, 'session-key'), 0);
  assert.equal(readWorkflowDraft(null, 'session-key', null), null);
});

test('rejects drafts that cannot be serialized', () => {
  const storage = new MemoryStorage();
  const employee: Record<string, unknown> = {};
  employee.circular = employee;

  assert.equal(
    writeWorkflowDraft(storage, 'session-key', null, { selectedEmployee: employee }),
    false,
  );
  assert.equal(storage.length, 0);
});

test('moves a valid pending draft without deleting it when the destination write fails', () => {
  const storage = new MemoryStorage();
  writeWorkflowDraft(storage, 'session-key', null, fullDraft);

  assert.equal(moveWorkflowDraft(storage, 'session-key', null, 'session-12'), true);
  assert.equal(readWorkflowDraft(storage, 'session-key', null), null);
  assert.deepEqual(readWorkflowDraft(storage, 'session-key', 'session-12'), fullDraft);

  const failingDestination = new MemoryStorage();
  writeWorkflowDraft(failingDestination, 'session-key', null, fullDraft);
  failingDestination.setItem = () => {
    throw new Error('quota');
  };

  assert.equal(moveWorkflowDraft(failingDestination, 'session-key', null, 'session-12'), false);
  assert.deepEqual(readWorkflowDraft(failingDestination, 'session-key', null), fullDraft);
});

test('does not move missing, malformed, or old-version entries', () => {
  const storage = new MemoryStorage();
  assert.equal(moveWorkflowDraft(storage, 'session-key', null, 'session-12'), false);

  const pendingKey = workflowDraftStorageKey('session-key', null);
  storage.setItem(pendingKey, '{bad');
  assert.equal(moveWorkflowDraft(storage, 'session-key', null, 'session-12'), false);

  storage.setItem(pendingKey, JSON.stringify({ version: 0, draft: fullDraft }));
  assert.equal(moveWorkflowDraft(storage, 'session-key', null, 'session-12'), false);
});

test('clears only the current versioned workflow-draft prefix', () => {
  const storage = new MemoryStorage();
  writeWorkflowDraft(storage, 'session-key', null, { profileText: 'pending' });
  writeWorkflowDraft(storage, 'session-key', 'session-12', { profileText: 'active' });
  writeWorkflowDraft(storage, 'another-key', 'session-12', { profileText: 'other' });
  storage.setItem('session-key:workflow-draft:v0:legacy', 'legacy');
  storage.setItem('session-key:unrelated', 'keep');

  assert.equal(clearWorkflowDrafts(storage, 'session-key'), 2);
  assert.equal(readWorkflowDraft(storage, 'session-key', null), null);
  assert.deepEqual(readWorkflowDraft(storage, 'another-key', 'session-12'), { profileText: 'other' });
  assert.equal(storage.getItem('session-key:workflow-draft:v0:legacy'), 'legacy');
  assert.equal(storage.getItem('session-key:unrelated'), 'keep');
});

test('removes only the current and legacy drafts for one exact session', () => {
  const storage = new MemoryStorage();
  writeWorkflowDraft(storage, 'session-key', 'session-12', { profileText: 'old current' });
  writeWorkflowDraft(storage, 'session-key', 'session-13', { profileText: 'other current' });
  writeWorkflowDraft(storage, 'session-key', null, { profileText: 'pending' });
  storage.setItem(legacyProfileDraftStorageKey('session-key', 'session-12'), 'old legacy');
  storage.setItem(legacyProfileDraftStorageKey('session-key', 'session-13'), 'other legacy');
  storage.setItem('session-key:workflow-draft:v0:session-12', 'old version');
  storage.setItem('another-key:workflow-draft:v1:session-12', 'other namespace');

  assert.equal(removeWorkflowSessionDrafts(storage, 'session-key', 'session-12'), 2);
  assert.equal(readWorkflowDraft(storage, 'session-key', 'session-12'), null);
  assert.equal(storage.getItem(legacyProfileDraftStorageKey('session-key', 'session-12')), null);
  assert.deepEqual(readWorkflowDraft(storage, 'session-key', 'session-13'), { profileText: 'other current' });
  assert.deepEqual(readWorkflowDraft(storage, 'session-key', null), { profileText: 'pending' });
  assert.equal(storage.getItem(legacyProfileDraftStorageKey('session-key', 'session-13')), 'other legacy');
  assert.equal(storage.getItem('session-key:workflow-draft:v0:session-12'), 'old version');
  assert.equal(storage.getItem('another-key:workflow-draft:v1:session-12'), 'other namespace');
});

test('clears every workflow version and legacy profile draft without touching unrelated keys', () => {
  const storage = new MemoryStorage();
  storage.setItem('session-key:workflow-draft:v0:session-0', 'v0');
  writeWorkflowDraft(storage, 'session-key', 'session-1', { profileText: 'v1' });
  storage.setItem('session-key:workflow-draft:v27:session-27', 'v27');
  storage.setItem(legacyProfileDraftStorageKey('session-key', null), 'legacy pending');
  storage.setItem(legacyProfileDraftStorageKey('session-key', 'session-1'), 'legacy active');
  storage.setItem('session-key:workflow-drafts:v1:near-collision', 'keep');
  storage.setItem('session-key:unrelated', 'keep');
  storage.setItem('another-key:workflow-draft:v1:session-1', 'other namespace');

  assert.equal(clearAllWorkflowDrafts(storage, 'session-key'), 5);
  assert.equal(storage.getItem('session-key:workflow-draft:v0:session-0'), null);
  assert.equal(readWorkflowDraft(storage, 'session-key', 'session-1'), null);
  assert.equal(storage.getItem('session-key:workflow-draft:v27:session-27'), null);
  assert.equal(storage.getItem(legacyProfileDraftStorageKey('session-key', null)), null);
  assert.equal(storage.getItem(legacyProfileDraftStorageKey('session-key', 'session-1')), null);
  assert.equal(storage.getItem('session-key:workflow-drafts:v1:near-collision'), 'keep');
  assert.equal(storage.getItem('session-key:unrelated'), 'keep');
  assert.equal(storage.getItem('another-key:workflow-draft:v1:session-1'), 'other namespace');
});

test('invalidates a stored session only for explicit 404 and 410 statuses', () => {
  assert.equal(shouldInvalidateStoredSession({ status: 404 }), true);
  assert.equal(shouldInvalidateStoredSession({ status: 410 }), true);
  assert.equal(shouldInvalidateStoredSession({ status: 401 }), false);
  assert.equal(shouldInvalidateStoredSession({ status: 500 }), false);
  assert.equal(shouldInvalidateStoredSession({ status: '404' }), false);
  assert.equal(shouldInvalidateStoredSession(new Error('404')), false);
  assert.equal(shouldInvalidateStoredSession(null), false);

  const throwingStatus = Object.create(null) as Record<string, unknown>;
  Object.defineProperty(throwingStatus, 'status', {
    get() {
      throw new Error('unavailable');
    },
  });
  assert.equal(shouldInvalidateStoredSession(throwingStatus), false);
});

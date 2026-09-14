import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

function source(path: string): string {
  return readFileSync(new URL(path, import.meta.url), 'utf8');
}

test('coalesces workflow draft persistence and flushes before leaving the page', () => {
  const workflow = source('../src/context/WorkflowContext.tsx');

  assert.match(workflow, /const WORKFLOW_DRAFT_PERSIST_DELAY_MS = 250;/u);
  assert.match(
    workflow,
    /workflowDraftPersistTimerRef\.current = window\.setTimeout\([\s\S]*?WORKFLOW_DRAFT_PERSIST_DELAY_MS/su,
  );
  assert.match(workflow, /window\.addEventListener\('pagehide', flushWorkflowDraftPersist\)/u);
  assert.match(workflow, /document\.addEventListener\('visibilitychange', flushWhenHidden\)/u);
  assert.doesNotMatch(workflow, /setWorkflowDraftState/u);
});

test('bounds streaming renders and chat-follow layout work', () => {
  const workflow = source('../src/context/WorkflowContext.tsx');
  const rehearsal = source('../src/pages/steps/RehearsalStep.tsx');

  assert.match(workflow, /const REHEARSAL_STREAM_RENDER_INTERVAL_MS = 50;/u);
  assert.match(
    workflow,
    /draftTimer = window\.setTimeout\([\s\S]*?REHEARSAL_STREAM_RENDER_INTERVAL_MS/su,
  );
  assert.match(rehearsal, /shouldFollowChatTailRef/u);
  assert.match(rehearsal, /node\.scrollHeight - node\.scrollTop - node\.clientHeight <= 64/u);
  assert.match(rehearsal, /const delay = rehearsalStreaming \? Math\.max\(0, 66 - elapsed\) : 0/u);
  assert.match(rehearsal, /messageInputMaxHeightRef/u);
  assert.match(rehearsal, /const scrollHeight = textarea\.scrollHeight;/u);
});

test('deduplicates ASR previews and avoids eager all-route prefetching', () => {
  const speech = source('../src/hooks/useSpeechToText.ts');
  const app = source('../src/App.tsx');
  const workspace = source('../src/pages/WorkspacePage.tsx');

  assert.match(speech, /if \(normalized === transcriptRef\.current\) return;/u);
  assert.doesNotMatch(app, /cancelPublicPrefetch|cancelWorkspacePrefetch/u);
  assert.doesNotMatch(workspace, /remainingSteps|cancelPrefetches/u);
});

test('does not animate workspace layout dimensions during sidebar changes', () => {
  const motion = source('../src/styles/motion.css');

  assert.match(motion, /\.app-shell\s*\{\s*transition: none !important;/u);
  assert.match(motion, /\.app-main\s*\{\s*transition: none !important;/u);
  assert.match(motion, /#screen-profile \.profile-layout\s*\{\s*transition: none !important;/u);
});

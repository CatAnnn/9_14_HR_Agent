import type {
  SessionState,
  StepKey,
  WorkflowStreamStatus,
} from '../types/domain';

export function isReportAccessible(
  session: SessionState | null,
  reportStatus: WorkflowStreamStatus,
): boolean {
  return Boolean(
    session?.rehearsal_ended_at
    || session?.coach_report_id
    || session?.stage === 'report_ready'
    || reportStatus !== 'idle',
  );
}

export function isStepUnlocked(
  step: StepKey,
  session: SessionState | null,
  reportAccessible: boolean,
): boolean {
  const stage = session?.stage || 'created';
  if (step === 'profile') return true;
  if (step === 'intent') return Boolean(session?.employee_profile)
    || ['profile_ready', 'setup_ready', 'guidance_ready', 'rehearsal', 'report_ready'].includes(stage);
  if (step === 'simulation') return Boolean(session?.intent)
    || ['setup_ready', 'guidance_ready', 'rehearsal', 'report_ready'].includes(stage);
  if (step === 'guidance') return Boolean(session?.setup_ready)
    || ['setup_ready', 'guidance_ready', 'rehearsal', 'report_ready'].includes(stage);
  if (step === 'rehearsal') return ['guidance_ready', 'rehearsal', 'report_ready'].includes(stage);
  if (step === 'report') return reportAccessible;
  return false;
}

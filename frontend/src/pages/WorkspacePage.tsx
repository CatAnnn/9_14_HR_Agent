import { Suspense, useCallback, useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { RouteLoading, RouteRedirect } from '../components/RouteLoading';
import { StepNav } from '../components/StepNav';
import { useWorkflowNavigation } from '../context/WorkflowContext';
import type { StepKey } from '../types/domain';
import { inferStepFromState, STEP_KEYS } from '../utils/format';
import { lazyRoute } from '../utils/lazyRoute';
import { scheduleIdlePrefetch } from '../utils/prefetch';
import { isReportAccessible } from '../utils/workflowSteps';
import { useLanguage } from '../i18n/LanguageContext';

const stepComponents = {
  profile: lazyRoute(() => import('./steps/ProfileStep')),
  intent: lazyRoute(() => import('./steps/IntentStep')),
  simulation: lazyRoute(() => import('./steps/SimulationStep')),
  guidance: lazyRoute(() => import('./steps/GuidanceStep')),
  rehearsal: lazyRoute(() => import('./steps/RehearsalStep')),
  report: lazyRoute(() => import('./steps/ReportStep')),
};

function isStep(value: string | undefined): value is StepKey {
  return Boolean(value && STEP_KEYS.includes(value as StepKey));
}

function readSidebarCollapsed(): boolean {
  try {
    return window.localStorage.getItem('hr-sidebar-collapsed') === 'true';
  } catch {
    return false;
  }
}

export default function WorkspacePage() {
  const { step: stepParam } = useParams();
  const navigate = useNavigate();
  const { translate } = useLanguage();
  const {
    bootstrap,
    session,
    hasIntentOptions,
    selectedIntentId,
    guidanceStatus,
    reportStatus,
    bootstrapStatus,
    ensureGuidance,
    ensureReport,
  } = useWorkflowNavigation();
  const [sidebarCollapsed, setSidebarCollapsed] = useState(readSidebarCollapsed);
  const inferred = inferStepFromState(session?.stage);
  const step: StepKey = isStep(stepParam) ? stepParam : inferred;
  const StepComponent = stepComponents[step];
  const toggleSidebar = useCallback(() => setSidebarCollapsed((value) => !value), []);
  const preloadStep = useCallback((candidate: StepKey) => {
    void stepComponents[candidate].preload().catch(() => undefined);
  }, []);
  const reportAccessible = isReportAccessible(session, reportStatus);
  const intentGate = session && hasIntentOptions
    ? Boolean(
      selectedIntentId
      && session.intent?.intent_id === selectedIntentId,
    )
    : undefined;

  useEffect(() => { void bootstrap().catch(() => undefined); }, [bootstrap]);

  useEffect(() => {
    try {
      window.localStorage.setItem('hr-sidebar-collapsed', String(sidebarCollapsed));
    } catch {
      // Sidebar state remains available in memory when browser storage is blocked.
    }
  }, [sidebarCollapsed]);

  useEffect(() => {
    preloadStep(step);
    if (bootstrapStatus !== 'ready') return undefined;
    const nextStep = STEP_KEYS[STEP_KEYS.indexOf(step) + 1];
    if (!nextStep) return undefined;
    return scheduleIdlePrefetch(() => stepComponents[nextStep].preload(), { delayMs: 450 });
  }, [bootstrapStatus, preloadStep, step]);

  useEffect(() => {
    if (bootstrapStatus === 'ready' && !stepParam && session) {
      navigate(`/app/${inferred}`, { replace: true });
    }
  }, [bootstrapStatus, inferred, navigate, session, stepParam]);

  useEffect(() => {
    if (bootstrapStatus !== 'ready') return;
    if (intentGate === false) return;
    if (step === 'guidance' && session?.setup_ready && guidanceStatus === 'idle') {
      void ensureGuidance().catch(() => undefined);
    }
    if (step === 'report' && reportAccessible) {
      void ensureReport().catch(() => undefined);
    }
  }, [bootstrapStatus, ensureGuidance, ensureReport, guidanceStatus, intentGate, reportAccessible, session?.setup_ready, step]);

  if (bootstrapStatus === 'error') {
    return (
      <main className="route-loading" role="alert" aria-live="assertive">
        <span>{translate('工作台载入失败，请重新加载')}</span>
        <button
          className="btn btn-primary"
          type="button"
          onClick={() => void bootstrap().catch(() => undefined)}
        >
          {translate('重新加载')}
        </button>
      </main>
    );
  }

  if (bootstrapStatus !== 'ready') return <RouteLoading label={translate('正在载入工作台')} />;

  if (stepParam && !isStep(stepParam)) return <RouteRedirect to={`/app/${inferred}`} />;

  if (
    intentGate === false
    && STEP_KEYS.indexOf(step) >= STEP_KEYS.indexOf('simulation')
  ) {
    return <RouteRedirect to="/app/intent" />;
  }

  if (step === 'report' && !reportAccessible) {
    return <RouteRedirect to="/app/rehearsal" />;
  }

  return (
    <div className={"app-shell" + (sidebarCollapsed ? " is-sidebar-collapsed" : "")} aria-live="polite">
      <StepNav
        current={step}
        session={session}
        collapsed={sidebarCollapsed}
        intentGate={intentGate}
        reportAccessible={reportAccessible}
        onToggle={toggleSidebar}
        onPreload={preloadStep}
      />
      <main className="app-main">
        <Suspense fallback={<RouteLoading compact label={translate('正在加载当前步骤')} />}>
          <StepComponent />
        </Suspense>
      </main>
    </div>
  );
}

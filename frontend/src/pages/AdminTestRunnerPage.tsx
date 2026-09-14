import { ArrowLeft, FlaskConical, Plus } from 'lucide-react';
import { lazy, Suspense, useCallback, useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { BoschSupergraphic } from '../components/BoschSupergraphic';
import { Brand } from '../components/Brand';
import { LanguageSelector } from '../components/LanguageSelector';
import { RouteLoading, RouteRedirect } from '../components/RouteLoading';
import { useWorkflow } from '../context/WorkflowContext';
import { intentName as localizedIntentName } from '../i18n/businessLabels';
import { useLanguage } from '../i18n/LanguageContext';
import { shouldReturnToAdminTestConfiguration } from '../utils/adminTestWorkflow';

const RehearsalStep = lazy(() => import('./steps/RehearsalStep'));
const ReportStep = lazy(() => import('./steps/ReportStep'));

type TestStep = 'rehearsal' | 'report';

function isTestStep(value: string | undefined): value is TestStep {
  return value === 'rehearsal' || value === 'report';
}

export default function AdminTestRunnerPage() {
  const { step } = useParams();
  const navigate = useNavigate();
  const { language, translate } = useLanguage();
  const { bootstrap, session, options, ensureReport } = useWorkflow();
  const [bootstrapFailed, setBootstrapFailed] = useState(false);

  const loadSession = useCallback(() => {
    setBootstrapFailed(false);
    void bootstrap().catch(() => {
      let storage: Storage | null = null;
      try {
        storage = window.localStorage;
      } catch {
        // When storage is unavailable, do not assume WorkflowContext invalidated the session.
      }
      if (shouldReturnToAdminTestConfiguration(storage)) {
        navigate('/admin/test-workflow', { replace: true });
        return;
      }
      setBootstrapFailed(true);
    });
  }, [bootstrap, navigate]);

  useEffect(() => {
    loadSession();
  }, [loadSession]);

  useEffect(() => {
    if (step === 'report' && session) void ensureReport().catch(() => undefined);
  }, [ensureReport, session, step]);

  if (!isTestStep(step)) return <RouteRedirect to="/admin/test-workflow" />;

  if (bootstrapFailed) {
    return (
      <main className="route-loading" role="alert" aria-live="assertive">
        <span>{translate('测试会话载入失败，请重新加载', 'The test session failed to load. Reload and try again.')}</span>
        <button className="btn btn-primary" type="button" onClick={loadSession}>
          {translate('重新加载', 'Reload')}
        </button>
      </main>
    );
  }

  const intentId = session?.intent?.intent_id || session?.intent?.id;
  const configuredIntentName = options.intents.find((item) => item.id === intentId)?.name
    || session?.intent?.name
    || intentId
    || translate('载入中', 'Loading');
  const intentName = localizedIntentName(intentId, configuredIntentName, language);

  return (
    <main className="admin-test-runner">
      <BoschSupergraphic />
      <header className="admin-test-runner-header">
        <Brand />
        <div className="admin-test-runner-title">
          <span><FlaskConical size={15} aria-hidden="true" /> {translate('管理员测试', 'Administrator test')}</span>
          <h1>{step === 'rehearsal' ? translate('多轮预演', 'Rehearsal') : translate('四维复盘', 'Four-dimension review')}</h1>
          <p>{intentName} · {translate('模拟员工档案', 'Synthetic employee profile')}</p>
        </div>
        <div className="admin-test-runner-actions">
          <LanguageSelector className="btn btn-secondary" compact />
          <button className="btn btn-secondary" type="button" onClick={() => navigate('/admin/test-workflow')}>
            <Plus size={17} aria-hidden="true" />
            {translate('新建测试', 'New test')}
          </button>
          <button className="btn btn-secondary" type="button" onClick={() => navigate('/admin')}>
            <ArrowLeft size={17} aria-hidden="true" />
            {translate('管理页面', 'Administration')}
          </button>
        </div>
      </header>

      <div className="admin-test-runner-content">
        {!session ? (
          <RouteLoading compact label={translate('正在载入测试会话', 'Loading test session')} />
        ) : (
          <Suspense fallback={<RouteLoading compact label={translate('正在加载测试模块', 'Loading test module')} />}>
            {step === 'rehearsal'
              ? <RehearsalStep reportPath="/admin/test-workflow/report" guidancePath={null} />
              : <ReportStep />}
          </Suspense>
        )}
      </div>
    </main>
  );
}

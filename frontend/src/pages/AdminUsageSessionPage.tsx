import '../styles/admin-page.css';
import {
  AlertTriangle,
  Check,
  Copy,
  FileText,
  LoaderCircle,
  RefreshCw,
} from 'lucide-react';
import { useCallback, useEffect, useState } from 'react';
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { authApi } from '../api/auth';
import { AdminHeader } from '../components/admin/AdminHeader';
import { AdminUsageSessionViewer } from '../components/admin/AdminUsageSessionDialog';
import { RouteRedirect } from '../components/RouteLoading';
import { useLanguage } from '../i18n/LanguageContext';
import { useAuthStore } from '../store/authStore';
import type { AdminUsageSessionDetail } from '../types/auth';
import {
  adminUsageSessionPath,
  isAdminUsageSection,
  type AdminUsageSection,
} from '../utils/adminUsageSession';
import { adminUsageReturnPath } from '../utils/adminUsageFilters';
import { userFacingErrorMessage } from '../utils/displayText';

type Translate = (source: string, english?: string) => string;

function translatedTemplate(
  translate: Translate,
  source: string,
  english: string,
  values: Readonly<Record<string, string | number>>,
): string {
  return translate(source, english).replace(/\{([a-z_]+)\}/giu, (placeholder, key: string) => (
    Object.prototype.hasOwnProperty.call(values, key) ? String(values[key]) : placeholder
  ));
}

async function copyText(value: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(value);
    return;
  }
  const textarea = document.createElement('textarea');
  textarea.value = value;
  textarea.style.position = 'fixed';
  textarea.style.opacity = '0';
  document.body.appendChild(textarea);
  textarea.select();
  const copied = document.execCommand('copy');
  textarea.remove();
  if (!copied) throw new Error('COPY_FAILED');
}

function AdminUsageSessionLoader() {
  const { sessionId = '', section } = useParams<{
    sessionId: string;
    section?: string;
  }>();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const { translate } = useLanguage();
  const [detail, setDetail] = useState<AdminUsageSessionDetail | null>(null);
  const [detailSessionId, setDetailSessionId] = useState('');
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<unknown | null>(null);
  const [errorSessionId, setErrorSessionId] = useState('');
  const [reloadKey, setReloadKey] = useState(0);
  const [copyState, setCopyState] = useState<'idle' | 'copied' | 'failed'>('idle');
  const [copySessionId, setCopySessionId] = useState('');
  const activeSection: AdminUsageSection = isAdminUsageSection(section)
    ? section
    : 'overview';
  const hasCanonicalSection = isAdminUsageSection(section);

  useEffect(() => {
    if (!sessionId || !hasCanonicalSection) return undefined;
    let active = true;
    setLoading(true);
    setLoadError(null);
    setErrorSessionId('');
    authApi.getUsageSessionDetail(sessionId)
      .then((response) => {
        if (!active) return;
        setDetail(response);
        setDetailSessionId(sessionId);
      })
      .catch((loadError: unknown) => {
        if (!active) return;
        setLoadError(loadError);
        setErrorSessionId(sessionId);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [hasCanonicalSection, reloadKey, sessionId]);

  useEffect(() => {
    if (copyState === 'idle') return undefined;
    const timer = window.setTimeout(() => setCopyState('idle'), 1800);
    return () => window.clearTimeout(timer);
  }, [copyState]);

  const changeSection = useCallback((nextSection: AdminUsageSection) => {
    navigate(adminUsageSessionPath(sessionId, nextSection, searchParams), { replace: true });
  }, [navigate, searchParams, sessionId]);

  const returnPath = adminUsageReturnPath(searchParams);
  const currentDetail = detailSessionId === sessionId ? detail : null;
  const currentLoadError = errorSessionId === sessionId ? loadError : null;
  const currentLoading = loading || (!currentDetail && !currentLoadError);
  const currentCopyState = copySessionId === sessionId ? copyState : 'idle';

  if (!sessionId) return <RouteRedirect to="/admin?section=usage" />;
  if (!hasCanonicalSection) {
    return (
      <RouteRedirect
        to={adminUsageSessionPath(sessionId, 'overview', searchParams)}
        replace
      />
    );
  }

  const displayName = currentDetail?.user_display_name
    || currentDetail?.user_email.split('@')[0]
    || translate('用户历史会话', 'Historical user session');

  return (
    <main className="admin-page admin-usage-page">
      <AdminHeader
        title={translate('管理平台', 'Administration')}
        identity={translate('用户历史会话', 'Historical user session')}
        detail={currentDetail?.user_email}
        badge={translate('只读', 'Read only')}
        backTo={returnPath}
        backLabel={translate('返回使用记录', 'Back to usage records')}
      />

      <div className="admin-usage-page-shell">
        <section className="admin-usage-page-heading" aria-labelledby="admin-usage-page-title">
          <div>
            <nav className="admin-usage-breadcrumb" aria-label={translate('当前位置', 'Breadcrumb')}>
              <Link to="/admin">{translate('管理平台', 'Administration')}</Link>
              <span aria-hidden="true">/</span>
              <Link to={returnPath}>{translate('使用记录', 'Usage records')}</Link>
              <span aria-hidden="true">/</span>
              <span aria-current="page">{translate('会话详情', 'Session details')}</span>
            </nav>
            <span className="admin-usage-read-only-badge">
              <FileText size={14} aria-hidden="true" />
              {translate('管理员只读视图', 'Administrator read-only view')}
            </span>
            <h2 id="admin-usage-page-title">
              {currentDetail
                ? translatedTemplate(
                  translate,
                  '{name} 的会话',
                  "{name}'s session",
                  { name: displayName },
                )
                : translate('会话详情', 'Session details')}
            </h2>
            <p>{translate(
              '显示用户本次保存的资料、谈前指导、预演对话和复盘报告，不会重新生成或修改内容。',
              'Shows the saved profile, guidance, rehearsal, and review without regenerating or changing content.',
            )}</p>
          </div>
          <button
            className={`admin-usage-session-copy is-${currentCopyState}`}
            type="button"
            title={sessionId}
            aria-live="polite"
            onClick={() => {
              setCopySessionId(sessionId);
              void copyText(sessionId)
                .then(() => setCopyState('copied'))
                .catch(() => setCopyState('failed'));
            }}
          >
            {currentCopyState === 'copied'
              ? <Check size={15} aria-hidden="true" />
              : <Copy size={15} aria-hidden="true" />}
            <span>
              {currentCopyState === 'copied'
                ? translate('已复制会话编号', 'Session ID copied')
                : currentCopyState === 'failed'
                  ? translate('复制失败', 'Copy failed')
                  : translate('复制会话编号', 'Copy session ID')}
            </span>
            <code>{sessionId}</code>
          </button>
        </section>

        <section className="admin-usage-page-card" aria-busy={currentLoading}>
          {currentLoading ? (
            <div className="admin-usage-detail-loading" role="status" aria-live="polite">
              <LoaderCircle className="admin-loading-icon" size={26} aria-hidden="true" />
              <p>{translate('正在读取这次使用记录…', 'Loading this usage record…')}</p>
            </div>
          ) : currentLoadError ? (
            <div className="admin-usage-detail-error" role="alert">
              <AlertTriangle size={27} aria-hidden="true" />
              <h3>{translate('无法读取使用详情', 'Unable to load usage details')}</h3>
              <p>{userFacingErrorMessage(
                currentLoadError,
                undefined,
                translate('无法读取这次使用详情。', 'Unable to load this usage record.'),
              )}</p>
              <button className="btn btn-secondary" type="button" onClick={() => setReloadKey((value) => value + 1)}>
                <RefreshCw size={16} aria-hidden="true" />
                {translate('重新加载', 'Reload')}
              </button>
            </div>
          ) : currentDetail ? (
            <AdminUsageSessionViewer
              detail={currentDetail}
              activeSection={activeSection}
              onSectionChange={changeSection}
            />
          ) : (
            <div className="admin-usage-detail-error" role="alert">
              <AlertTriangle size={27} aria-hidden="true" />
              <p>{translate('没有可展示的使用详情。', 'No usage details are available.')}</p>
            </div>
          )}
        </section>
      </div>
    </main>
  );
}

export default function AdminUsageSessionPage() {
  const { user } = useAuthStore();

  if (user?.role !== 'admin') return <RouteRedirect to="/" />;
  return <AdminUsageSessionLoader />;
}

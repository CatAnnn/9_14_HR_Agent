import {
  ArrowLeft,
  FlaskConical,
  LoaderCircle,
  LogOut,
} from 'lucide-react';
import { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useLanguage } from '../../i18n/LanguageContext';
import { useAuthStore } from '../../store/authStore';
import { Brand } from '../Brand';
import { LanguageSelector } from '../LanguageSelector';

interface AdminHeaderProps {
  title: string;
  identity: string;
  detail?: string | null;
  badge: string;
  backTo?: string;
  backLabel?: string;
  showWorkflowTesting?: boolean;
}

export function AdminHeader({
  title,
  identity,
  detail,
  badge,
  backTo,
  backLabel,
  showWorkflowTesting = false,
}: AdminHeaderProps) {
  const navigate = useNavigate();
  const { translate } = useLanguage();
  const { logout } = useAuthStore();
  const [signingOut, setSigningOut] = useState(false);

  const signOut = async () => {
    if (signingOut) return;
    setSigningOut(true);
    await logout();
    navigate('/login', { replace: true });
  };

  return (
    <header className="admin-header">
      <Link
        className="admin-brand-home"
        to="/"
        title={translate('返回主页', 'Back to home')}
        aria-label={translate('返回主页', 'Back to home')}
      >
        <Brand />
      </Link>
      <div className="admin-page-title">
        <h1>{title}</h1>
        <p>
          <span>{identity}</span>
          {detail && <span className="admin-page-title-detail">{detail}</span>}
          <small>{badge}</small>
        </p>
      </div>
      <div className="admin-header-actions">
        {backTo && (
          <Link
            className="admin-header-action admin-header-back"
            to={backTo}
            title={backLabel}
            aria-label={backLabel}
          >
            <ArrowLeft size={17} aria-hidden="true" />
            <span>{backLabel}</span>
          </Link>
        )}
        <LanguageSelector className="admin-header-action admin-language-selector" compact />
        {showWorkflowTesting && (
          <Link
            className="admin-header-action"
            to="/admin/test-workflow"
            title={translate('流程测试', 'Workflow testing')}
            aria-label={translate('进入流程测试', 'Open workflow testing')}
          >
            <FlaskConical size={17} aria-hidden="true" />
            <span>{translate('流程测试', 'Workflow testing')}</span>
          </Link>
        )}
        <button
          className="admin-header-action admin-header-sign-out"
          type="button"
          onClick={() => void signOut()}
          disabled={signingOut}
          title={translate('退出登录', 'Sign out')}
          aria-label={translate('退出登录', 'Sign out')}
        >
          {signingOut
            ? <LoaderCircle className="admin-loading-icon" size={17} aria-hidden="true" />
            : <LogOut size={17} aria-hidden="true" />}
          <span>{translate('退出', 'Sign out')}</span>
        </button>
      </div>
    </header>
  );
}

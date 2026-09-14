import { useEffect, useRef, useState } from 'react';
import { CircleHelp, LogOut, MoreHorizontal, Settings2, ShieldCheck, UserRound } from 'lucide-react';
import { useNavigate } from 'react-router-dom';
import { useOptionalWorkflowPageGuide } from './WorkflowPageGuide';
import { LanguageSelector } from './LanguageSelector';
import { useLanguage } from '../i18n/LanguageContext';
import { useAuthStore } from '../store/authStore';

interface AccountActionsProps {
  compact?: boolean;
  variant?: 'default' | 'sidebar' | 'display-name';
}

export function AccountActions({ compact = false, variant = 'default' }: AccountActionsProps) {
  const navigate = useNavigate();
  const { user, logout } = useAuthStore();
  const { translate } = useLanguage();
  const workflowGuide = useOptionalWorkflowPageGuide();
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRootRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!menuOpen) return;

    const closeOnPointerDown = (event: PointerEvent) => {
      if (!menuRootRef.current?.contains(event.target as Node)) setMenuOpen(false);
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setMenuOpen(false);
    };

    window.addEventListener('pointerdown', closeOnPointerDown);
    window.addEventListener('keydown', closeOnEscape);
    return () => {
      window.removeEventListener('pointerdown', closeOnPointerDown);
      window.removeEventListener('keydown', closeOnEscape);
    };
  }, [menuOpen]);

  if (!user) return null;

  const isGuest = user.role === 'guest';
  const displayName = isGuest
    ? translate('游客', 'Guest')
    : (user.display_name || user.email.split('@')[0]);

  const signOut = async () => {
    setMenuOpen(false);
    if (!isGuest) await logout();
    navigate('/login', { replace: true });
  };

  const openAccountDestination = (destination: 'profile' | 'settings') => {
    setMenuOpen(false);
    if (destination === 'profile') {
      navigate('/app/profile');
      return;
    }
    navigate(user.role === 'admin' ? '/admin' : '/app/profile');
  };

  if (variant === 'display-name') {
    return (
      <div
        ref={menuRootRef}
        className={`landing-account-menu${menuOpen ? ' is-open' : ''}`}
      >
        <button
          className="account-display-name"
          type="button"
          title={displayName}
          aria-label={`${translate('打开账号菜单')}: ${displayName}`}
          aria-haspopup="menu"
          aria-expanded={menuOpen}
          onClick={() => setMenuOpen((value) => !value)}
        >
          <span>{displayName}</span>
        </button>

        {menuOpen && (
          <div className="landing-account-popover" role="menu" aria-label={translate('账号菜单')}>
            <button type="button" role="menuitem" onClick={() => openAccountDestination('profile')}>
              <UserRound aria-hidden="true" />
              <span>{translate('个人资料')}</span>
            </button>
            <button type="button" role="menuitem" onClick={() => openAccountDestination('settings')}>
              <Settings2 aria-hidden="true" />
              <span>{translate('设置')}</span>
            </button>
            {user.role === 'admin' && (
              <button
                type="button"
                role="menuitem"
                onClick={() => {
                  setMenuOpen(false);
                  navigate('/admin');
                }}
              >
                <ShieldCheck aria-hidden="true" />
                <span>{translate('管理员')}</span>
              </button>
            )}
            <LanguageSelector className="landing-account-language-selector" compact />
            <div className="landing-account-divider" />
            <button
              className={isGuest ? undefined : 'is-danger'}
              type="button"
              role="menuitem"
              onClick={() => void signOut()}
            >
              <LogOut aria-hidden="true" />
              <span>{translate(isGuest ? '登录账号' : '退出登录', isGuest ? 'Sign in' : 'Log out')}</span>
            </button>
          </div>
        )}
      </div>
    );
  }

  const initials = displayName
    .split(/[\s._-]+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase())
    .join('') || 'U';

  if (variant === 'sidebar') {
    return (
      <div
        ref={menuRootRef}
        className={`sidebar-account-menu${compact ? ' is-compact' : ''}${menuOpen ? ' is-open' : ''}`}
      >
        {menuOpen && (
          <div className="sidebar-account-popover" role="menu" aria-label={translate('账号菜单')}>
            <div className="sidebar-account-popover-head">
              <span className="sidebar-account-avatar" aria-hidden="true">{initials}</span>
              <span>
                <strong>{displayName}</strong>
                <small>{translate(
                  user.role === 'admin' ? '管理员' : (isGuest ? '游客模式' : '授权用户'),
                  user.role === 'admin' ? 'Administrator' : (isGuest ? 'Guest mode' : 'Authorized user'),
                )}</small>
              </span>
            </div>
            <div className="sidebar-account-divider" />
            {user.role === 'admin' && (
              <button
                type="button"
                role="menuitem"
                className="sidebar-account-menu-item"
                onClick={() => {
                  setMenuOpen(false);
                  navigate('/admin');
                }}
              >
                <ShieldCheck aria-hidden="true" />
                <span>{translate('管理账号')}</span>
              </button>
            )}
            {workflowGuide?.available && (
              <button
                type="button"
                role="menuitem"
                className="sidebar-account-menu-item"
                onClick={() => {
                  setMenuOpen(false);
                  workflowGuide.restartCurrentPage();
                }}
              >
                <CircleHelp aria-hidden="true" />
                <span>{translate('页面介绍', 'Page introduction')}</span>
              </button>
            )}
            <LanguageSelector className="sidebar-account-menu-item" compact />
            <button
              type="button"
              role="menuitem"
              className={`sidebar-account-menu-item${isGuest ? '' : ' is-danger'}`}
              onClick={() => void signOut()}
            >
              <LogOut aria-hidden="true" />
              <span>{translate(isGuest ? '登录账号' : '退出登录', isGuest ? 'Sign in' : 'Log out')}</span>
            </button>
          </div>
        )}

        <button
          className="sidebar-account-trigger"
          type="button"
          aria-haspopup="menu"
          aria-expanded={menuOpen}
          aria-label={compact ? `${translate('打开账号菜单')}: ${displayName}` : translate('打开账号菜单')}
          data-tooltip={compact ? displayName : undefined}
          onClick={() => setMenuOpen((value) => !value)}
        >
          <span className="sidebar-account-avatar" aria-hidden="true">{initials}</span>
          {!compact && (
            <>
              <span className="sidebar-account-copy">
                <strong>{displayName}</strong>
                <small>{translate(
                  user.role === 'admin' ? '管理员' : (isGuest ? '游客模式' : '授权用户'),
                  user.role === 'admin' ? 'Administrator' : (isGuest ? 'Guest mode' : 'Authorized user'),
                )}</small>
              </span>
              <span className="sidebar-account-more" aria-hidden="true">
                <MoreHorizontal aria-hidden="true" />
              </span>
            </>
          )}
        </button>
      </div>
    );
  }

  return (
    <div className={`account-actions ${compact ? 'is-compact' : ''}`}>
      <div className="account-identity" title={user.email}>
        <span className="account-avatar" aria-hidden="true">
          <UserRound aria-hidden="true" />
        </span>
        {!compact && (
          <span className="account-copy">
            <strong>{displayName}</strong>
            <small>{translate(
              user.role === 'admin' ? '管理员' : (isGuest ? '游客模式' : '授权用户'),
              user.role === 'admin' ? 'Administrator' : (isGuest ? 'Guest mode' : 'Authorized user'),
            )}</small>
          </span>
        )}
      </div>
      <div className="account-command-group">
        {user.role === 'admin' && (
          <button
            className="icon-button"
            type="button"
            onClick={() => navigate('/admin')}
            title={translate('账号与白名单')}
            aria-label={translate('账号与白名单')}
          >
            <ShieldCheck aria-hidden="true" />
          </button>
        )}
        <button
          className="icon-button"
          type="button"
          onClick={() => void signOut()}
          title={translate(isGuest ? '登录账号' : '退出登录', isGuest ? 'Sign in' : 'Log out')}
          aria-label={translate(isGuest ? '登录账号' : '退出登录', isGuest ? 'Sign in' : 'Log out')}
        >
          <LogOut aria-hidden="true" />
        </button>
      </div>
    </div>
  );
}

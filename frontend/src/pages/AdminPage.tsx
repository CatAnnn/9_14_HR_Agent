import '../styles/admin-page.css';
import {
  AlertTriangle,
  Eye,
  EyeOff,
  History,
  KeyRound,
  LoaderCircle,
  Plus,
  RefreshCw,
  Search,
  Trash2,
  UserPlus,
  UsersRound,
  X,
} from 'lucide-react';
import {
  type FormEvent,
  type KeyboardEvent as ReactKeyboardEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { authApi } from '../api/auth';
import { AdminExportPanel } from '../components/admin/AdminExportPanel';
import { AdminHeader } from '../components/admin/AdminHeader';
import { RouteRedirect } from '../components/RouteLoading';
import { useDialogFocus } from '../hooks/useDialogFocus';
import { useLanguage } from '../i18n/LanguageContext';
import { useAuthStore } from '../store/authStore';
import type { AdminAccount } from '../types/auth';
import {
  adminAccountMatchesFilter,
  canAdminAccountLogin,
  isDisabledAdminAccount,
  isInactiveRegisteredAccount,
  normalizedAdminEmail,
  removeAdminAccount,
  upsertAdminAccount,
  type AdminAccountFilter,
} from '../utils/adminAccounts';
import { userFacingErrorMessage } from '../utils/displayText';

type ProvisionMode = 'whitelist' | 'account';
type AdminSection = 'accounts' | 'exports';
type AccountCatalogStatus = 'idle' | 'loading' | 'ready' | 'error';
type PasswordDialogMode = 'reset' | 'restore';
type Translate = (source: string, english?: string) => string;

type AdminValidationErrorCode = 'password_requirements' | 'password_mismatch';
type AdminRequestErrorCode =
  | 'account_operation'
  | 'account_restore'
  | 'password_reset'
  | 'account_delete'
  | 'allowlist_update';
type AdminFeedbackError =
  | { kind: 'validation'; code: AdminValidationErrorCode }
  | { kind: 'request'; cause: unknown; fallbackCode: AdminRequestErrorCode };
type AdminFeedbackMessageCode =
  | 'allowlist_restored_account_inactive'
  | 'allowlist_added'
  | 'account_restored'
  | 'account_created'
  | 'password_reset'
  | 'account_deleted';

interface PasswordDialogState {
  mode: PasswordDialogMode;
  account: AdminAccount;
}

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

function validPassword(value: string) {
  return /^\d{8,}$/.test(value) || (!/^\d+$/.test(value) && value.length >= 15);
}

function adminValidationErrorMessage(
  translate: Translate,
  code: AdminValidationErrorCode,
): string {
  if (code === 'password_requirements') {
    return translate(
      '密码需为至少 8 位数字，或至少 15 位字符。',
      'Use at least 8 digits or at least 15 characters.',
    );
  }
  return translate('两次输入的密码不一致。', 'The passwords do not match.');
}

function adminRequestErrorFallback(
  translate: Translate,
  code: AdminRequestErrorCode,
): string {
  switch (code) {
    case 'account_restore':
      return translate('账号恢复失败。', 'The account could not be restored.');
    case 'password_reset':
      return translate('密码重置失败。', 'The password reset failed.');
    case 'account_delete':
      return translate('账号删除失败。', 'The account could not be deleted.');
    case 'allowlist_update':
      return translate('白名单更新失败。', 'The allowlist could not be updated.');
    default:
      return translate('账号处理失败。', 'The account operation failed.');
  }
}

function adminFeedbackErrorMessage(
  translate: Translate,
  feedback: AdminFeedbackError | null,
): string {
  if (!feedback) return '';
  if (feedback.kind === 'validation') {
    return adminValidationErrorMessage(translate, feedback.code);
  }
  return userFacingErrorMessage(
    feedback.cause,
    undefined,
    adminRequestErrorFallback(translate, feedback.fallbackCode),
  );
}

function adminFeedbackMessage(
  translate: Translate,
  code: AdminFeedbackMessageCode | null,
): string {
  switch (code) {
    case 'allowlist_restored_account_inactive':
      return translate(
        '白名单已恢复，但账号仍处于停用状态；请使用“恢复账号”设置新密码后再登录。',
        'Allowlist access was restored, but the account is still inactive. Use Restore account to set a new password before signing in.',
      );
    case 'allowlist_added':
      return translate(
        '邮箱已加入白名单，可由用户自行注册。',
        'The email has been added to the allowlist and the user can now register.',
      );
    case 'account_restored':
      return translate(
        '账号已恢复并设置新密码。',
        'The account was restored with a new password.',
      );
    case 'account_created':
      return translate('授权账号已创建。', 'The authorized account has been created.');
    case 'password_reset':
      return translate(
        '密码已重置，该账号的已有登录已失效。',
        'The password has been reset and existing sessions for this account are now invalid.',
      );
    case 'account_deleted':
      return translate(
        '白名单授权已删除，账号已停用。',
        'The allowlist authorization was removed and the account was disabled.',
      );
    default:
      return '';
  }
}

export default function AdminPage() {
  const { translate } = useLanguage();
  const { user } = useAuthStore();
  const [searchParams, setSearchParams] = useSearchParams();
  const [accounts, setAccounts] = useState<AdminAccount[]>([]);
  const activeSection: AdminSection = searchParams.get('section') === 'usage'
    ? 'exports'
    : 'accounts';
  const [accountSearch, setAccountSearch] = useState('');
  const [accountFilter, setAccountFilter] = useState<AdminAccountFilter>('all');
  const [accountCatalogStatus, setAccountCatalogStatus] = useState<AccountCatalogStatus>('idle');
  const [accountLoadError, setAccountLoadError] = useState<unknown>(null);
  const [mode, setMode] = useState<ProvisionMode>('whitelist');
  const [email, setEmail] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [password, setPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [passwordDialog, setPasswordDialog] = useState<PasswordDialogState | null>(null);
  const [resetPassword, setResetPassword] = useState('');
  const [resetConfirmPassword, setResetConfirmPassword] = useState('');
  const [resetPasswordVisible, setResetPasswordVisible] = useState(false);
  const [passwordDialogError, setPasswordDialogError] = useState<AdminFeedbackError | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<AdminAccount | null>(null);
  const [deleteDialogError, setDeleteDialogError] = useState<AdminFeedbackError | null>(null);
  const [activeAccountAction, setActiveAccountAction] = useState<string | null>(null);
  const [error, setError] = useState<AdminFeedbackError | null>(null);
  const [message, setMessage] = useState<AdminFeedbackMessageCode | null>(null);
  const [busy, setBusy] = useState(false);
  const accountRequestRef = useRef(0);
  const passwordInputRef = useRef<HTMLInputElement>(null);
  const deleteCancelRef = useRef<HTMLButtonElement>(null);
  const accountListHeadingRef = useRef<HTMLHeadingElement>(null);
  const loadingAccounts = accountCatalogStatus === 'loading'
    || (accountCatalogStatus === 'idle' && activeSection === 'accounts');

  const loadAccounts = useCallback(async () => {
    const requestId = accountRequestRef.current + 1;
    accountRequestRef.current = requestId;
    setAccountCatalogStatus('loading');
    setAccountLoadError(null);
    try {
      const result = await authApi.listAccounts();
      if (accountRequestRef.current !== requestId) return;
      setAccounts(result.items);
      setAccountCatalogStatus('ready');
    } catch (loadError) {
      if (accountRequestRef.current !== requestId) return;
      setAccountLoadError(() => loadError);
      setAccountCatalogStatus('error');
    }
  }, []);

  const filteredAccounts = useMemo(() => {
    const query = accountSearch.trim().toLowerCase();
    return accounts.filter((account) => {
      if (!adminAccountMatchesFilter(account, accountFilter)) return false;
      if (!query) return true;
      return [
        account.display_name || '',
        account.email,
        account.role,
        account.registered
          ? translate('已注册', 'Registered')
          : translate('待注册', 'Pending registration'),
        isInactiveRegisteredAccount(account)
          ? translate('账号已停用', 'Account inactive')
          : canAdminAccountLogin(account)
            ? translate('可登录', 'Login enabled')
            : account.whitelist_enabled
              ? translate('可注册', 'Registration enabled')
              : translate('已停用', 'Disabled'),
      ].some((value) => value.toLowerCase().includes(query));
    });
  }, [accountFilter, accountSearch, accounts, translate]);

  const registeredCount = useMemo(
    () => accounts.filter((account) => account.registered).length,
    [accounts],
  );
  const enabledCount = useMemo(
    () => accounts.filter(canAdminAccountLogin).length,
    [accounts],
  );
  const pendingCount = accounts.length - registeredCount;
  const disabledCount = useMemo(
    () => accounts.filter(isDisabledAdminAccount).length,
    [accounts],
  );

  useEffect(() => {
    if (
      user?.role === 'admin'
      && activeSection === 'accounts'
      && accountCatalogStatus === 'idle'
      && accountRequestRef.current === 0
    ) {
      void loadAccounts();
    }
  }, [accountCatalogStatus, activeSection, loadAccounts, user?.role]);

  const closePasswordDialog = useCallback(() => {
    setPasswordDialog(null);
    setResetPassword('');
    setResetConfirmPassword('');
    setResetPasswordVisible(false);
    setPasswordDialogError(null);
  }, []);

  const closeDeleteDialog = useCallback(() => {
    setDeleteTarget(null);
    setDeleteDialogError(null);
  }, []);

  const passwordDialogRef = useDialogFocus<HTMLFormElement, HTMLInputElement>({
    active: Boolean(passwordDialog),
    closeDisabled: busy,
    initialFocusRef: passwordInputRef,
    onClose: closePasswordDialog,
  });
  const deleteDialogRef = useDialogFocus<HTMLElement, HTMLButtonElement>({
    active: Boolean(deleteTarget),
    closeDisabled: busy,
    initialFocusRef: deleteCancelRef,
    onClose: closeDeleteDialog,
    returnFocusFallbackRef: accountListHeadingRef,
  });

  const accountLoadErrorMessage = accountLoadError === null
    ? ''
    : userFacingErrorMessage(
      accountLoadError,
      undefined,
      translate('无法读取白名单账号。', 'Unable to load allowlisted accounts.'),
    );
  const accountActionErrorMessage = adminFeedbackErrorMessage(translate, error);
  const accountFeedbackError = accountActionErrorMessage || accountLoadErrorMessage;
  const accountFeedbackMessage = adminFeedbackMessage(translate, message);
  const passwordDialogErrorMessage = adminFeedbackErrorMessage(translate, passwordDialogError);
  const deleteDialogErrorMessage = adminFeedbackErrorMessage(translate, deleteDialogError);

  if (user?.role !== 'admin') return <RouteRedirect to="/" />;

  const submitProvision = async (event: FormEvent) => {
    event.preventDefault();
    if (busy || loadingAccounts) return;
    setError(null);
    setMessage(null);
    if (mode === 'account') {
      if (!validPassword(password)) {
        setError({ kind: 'validation', code: 'password_requirements' });
        return;
      }
      if (password !== confirmPassword) {
        setError({ kind: 'validation', code: 'password_mismatch' });
        return;
      }
    }
    setBusy(true);
    setActiveAccountAction('provision');
    try {
      const existingAccount = accounts.find(
        (account) => normalizedAdminEmail(account.email) === normalizedAdminEmail(email),
      );
      let updatedAccount: AdminAccount;
      if (mode === 'whitelist') {
        updatedAccount = await authApi.updateWhitelist(email, true);
        if (isInactiveRegisteredAccount(updatedAccount)) {
          setMessage('allowlist_restored_account_inactive');
        } else {
          setMessage('allowlist_added');
        }
      } else {
        updatedAccount = await authApi.createAccount(email, password, displayName || undefined);
        setMessage(isInactiveRegisteredAccount(existingAccount || updatedAccount)
          ? 'account_restored'
          : 'account_created');
      }
      setAccounts((current) => upsertAdminAccount(current, updatedAccount));
      setEmail('');
      setDisplayName('');
      setPassword('');
      setConfirmPassword('');
    } catch (err) {
      setError({ kind: 'request', cause: err, fallbackCode: 'account_operation' });
    } finally {
      setBusy(false);
      setActiveAccountAction(null);
    }
  };

  const openPasswordDialog = (account: AdminAccount, dialogMode: PasswordDialogMode) => {
    setPasswordDialog({ account, mode: dialogMode });
    setResetPassword('');
    setResetConfirmPassword('');
    setResetPasswordVisible(false);
    setPasswordDialogError(null);
    setError(null);
    setMessage(null);
  };

  const submitAccountPassword = async (event: FormEvent) => {
    event.preventDefault();
    if (!passwordDialog || busy) return;
    setPasswordDialogError(null);
    setMessage(null);
    if (!validPassword(resetPassword)) {
      setPasswordDialogError({ kind: 'validation', code: 'password_requirements' });
      return;
    }
    if (resetPassword !== resetConfirmPassword) {
      setPasswordDialogError({ kind: 'validation', code: 'password_mismatch' });
      return;
    }
    const { account, mode: dialogMode } = passwordDialog;
    const actionPrefix = dialogMode === 'restore' ? 'restore' : 'reset';
    setBusy(true);
    setActiveAccountAction(`${actionPrefix}:${account.email}`);
    try {
      const updatedAccount = dialogMode === 'restore'
        ? await authApi.createAccount(
          account.email,
          resetPassword,
          account.display_name || undefined,
        )
        : await authApi.resetPassword(account.email, resetPassword);
      setAccounts((current) => upsertAdminAccount(current, updatedAccount));
      closePasswordDialog();
      setMessage(dialogMode === 'restore'
        ? 'account_restored'
        : 'password_reset');
    } catch (err) {
      setPasswordDialogError({
        kind: 'request',
        cause: err,
        fallbackCode: dialogMode === 'restore' ? 'account_restore' : 'password_reset',
      });
    } finally {
      setBusy(false);
      setActiveAccountAction(null);
    }
  };

  const deleteAccount = async (account: AdminAccount) => {
    if (busy) return;
    setError(null);
    setMessage(null);
    setDeleteDialogError(null);
    setBusy(true);
    setActiveAccountAction(`delete:${account.email}`);
    try {
      await authApi.deleteAccount(account.email);
      setAccounts((current) => removeAdminAccount(current, account.email));
      closeDeleteDialog();
      setMessage('account_deleted');
    } catch (err) {
      setDeleteDialogError({ kind: 'request', cause: err, fallbackCode: 'account_delete' });
    } finally {
      setBusy(false);
      setActiveAccountAction(null);
    }
  };

  const toggleWhitelist = async (account: AdminAccount) => {
    if (busy || loadingAccounts || isInactiveRegisteredAccount(account)) return;
    setError(null);
    setMessage(null);
    setBusy(true);
    setActiveAccountAction(`toggle:${account.email}`);
    try {
      const updatedAccount = await authApi.updateWhitelist(
        account.email,
        !account.whitelist_enabled,
      );
      setAccounts((current) => upsertAdminAccount(current, updatedAccount));
    } catch (err) {
      setError({ kind: 'request', cause: err, fallbackCode: 'allowlist_update' });
    } finally {
      setBusy(false);
      setActiveAccountAction(null);
    }
  };

  const switchSection = (section: AdminSection) => {
    const nextSearchParams = new URLSearchParams(searchParams);
    if (section === 'exports') nextSearchParams.set('section', 'usage');
    else nextSearchParams.delete('section');
    setSearchParams(nextSearchParams, { replace: true });
    setError(null);
    setMessage(null);
  };

  const handleSectionKeyDown = (
    event: ReactKeyboardEvent<HTMLButtonElement>,
    section: AdminSection,
  ) => {
    const sections: AdminSection[] = ['accounts', 'exports'];
    const currentIndex = sections.indexOf(section);
    let nextIndex: number;
    if (event.key === 'ArrowRight') nextIndex = (currentIndex + 1) % sections.length;
    else if (event.key === 'ArrowLeft') nextIndex = (currentIndex - 1 + sections.length) % sections.length;
    else if (event.key === 'Home') nextIndex = 0;
    else if (event.key === 'End') nextIndex = sections.length - 1;
    else return;

    event.preventDefault();
    const nextSection = sections[nextIndex];
    switchSection(nextSection);
    window.requestAnimationFrame(() => document.getElementById(`admin-tab-${nextSection}`)?.focus());
  };

  return (
    <main className="admin-page">
      <AdminHeader
        title={translate('管理平台', 'Administration')}
        identity={user?.display_name || user?.email.split('@')[0] || translate('管理员', 'Administrator')}
        detail={user?.email}
        badge={translate('管理员', 'Administrator')}
        showWorkflowTesting
      />

      <div className="admin-shell">
        <nav
          className="admin-view-tabs"
          role="tablist"
          aria-label={translate('管理功能', 'Administration functions')}
        >
          <button
            id="admin-tab-accounts"
            type="button"
            role="tab"
            aria-selected={activeSection === 'accounts'}
            aria-controls="admin-panel-accounts"
            tabIndex={activeSection === 'accounts' ? 0 : -1}
            onClick={() => switchSection('accounts')}
            onKeyDown={(event) => handleSectionKeyDown(event, 'accounts')}
          >
            <UsersRound size={18} aria-hidden="true" />
            {translate('账号管理', 'Account management')}
          </button>
          <button
            id="admin-tab-exports"
            type="button"
            role="tab"
            aria-selected={activeSection === 'exports'}
            aria-controls="admin-panel-exports"
            tabIndex={activeSection === 'exports' ? 0 : -1}
            onClick={() => switchSection('exports')}
            onKeyDown={(event) => handleSectionKeyDown(event, 'exports')}
          >
            <History size={18} aria-hidden="true" />
            {translate('使用记录', 'Usage records')}
          </button>
        </nav>

        {activeSection === 'accounts' && (accountFeedbackError || accountFeedbackMessage) && (
          <div
            className={accountFeedbackError ? 'auth-error admin-feedback' : 'auth-success admin-feedback'}
            role={accountFeedbackError ? 'alert' : 'status'}
          >
            {accountFeedbackError || accountFeedbackMessage}
          </div>
        )}

        {activeSection === 'accounts' ? (
          <section
            id="admin-panel-accounts"
            className="admin-account-workspace"
            role="tabpanel"
            aria-labelledby="admin-tab-accounts"
          >
            <section
              className="admin-account-overview"
              aria-label={translate('账号状态概览', 'Account status overview')}
              aria-busy={loadingAccounts}
            >
              <button
                type="button"
                className={accountFilter === 'all' ? 'is-active' : ''}
                aria-pressed={accountFilter === 'all'}
                onClick={() => setAccountFilter('all')}
              >
                <span>{translate('授权总数', 'Authorized')}</span>
                <strong>{accounts.length}</strong>
              </button>
              <button
                type="button"
                className={accountFilter === 'registered' ? 'is-active' : ''}
                aria-pressed={accountFilter === 'registered'}
                onClick={() => setAccountFilter('registered')}
              >
                <span>{translate('已注册', 'Registered')}</span>
                <strong>{registeredCount}</strong>
              </button>
              <button
                type="button"
                className={accountFilter === 'enabled' ? 'is-active' : ''}
                aria-pressed={accountFilter === 'enabled'}
                onClick={() => setAccountFilter('enabled')}
              >
                <span>{translate('可登录', 'Login enabled')}</span>
                <strong>{enabledCount}</strong>
              </button>
              <button
                type="button"
                className={accountFilter === 'pending' ? 'is-active' : ''}
                aria-pressed={accountFilter === 'pending'}
                onClick={() => setAccountFilter('pending')}
              >
                <span>{translate('待注册', 'Pending')}</span>
                <strong>{pendingCount}</strong>
              </button>
            </section>
            <form className="admin-provision-panel" onSubmit={submitProvision}>
              <header className="admin-panel-heading">
                <h2>{translate('新增授权', 'Add authorization')}</h2>
              </header>
              <div
                className="segmented-control"
                aria-label={translate('授权方式', 'Authorization method')}
              >
                <button
                  type="button"
                  className={mode === 'whitelist' ? 'active' : ''}
                  aria-pressed={mode === 'whitelist'}
                  onClick={() => setMode('whitelist')}
                >
                  {translate('仅加入白名单', 'Allowlist only')}
                </button>
                <button
                  type="button"
                  className={mode === 'account' ? 'active' : ''}
                  aria-pressed={mode === 'account'}
                  onClick={() => setMode('account')}
                >
                  {translate('直接创建账号', 'Create account')}
                </button>
              </div>
              <label>
                <span>{translate('邮箱', 'Email')}</span>
                <input
                  type="email"
                  value={email}
                  placeholder="name@company.com"
                  autoComplete="email"
                  spellCheck={false}
                  onChange={(event) => setEmail(event.target.value)}
                  required
                />
              </label>
              {mode === 'account' && (
                <>
                  <label>
                    <span>{translate('显示名称', 'Display name')}</span>
                    <input
                      value={displayName}
                      placeholder={translate('选填', 'Optional')}
                      onChange={(event) => setDisplayName(event.target.value)}
                    />
                  </label>
                  <label>
                    <span>{translate('密码', 'Password')}</span>
                    <input
                      type="password"
                      value={password}
                      onChange={(event) => setPassword(event.target.value)}
                      autoComplete="new-password"
                      required
                    />
                  </label>
                  <label>
                    <span>{translate('确认密码', 'Confirm password')}</span>
                    <input
                      type="password"
                      value={confirmPassword}
                      onChange={(event) => setConfirmPassword(event.target.value)}
                      autoComplete="new-password"
                      required
                    />
                  </label>
                  <small>{translate(
                    '至少 8 位数字，或至少 15 位字符',
                    'At least 8 digits or at least 15 characters',
                  )}</small>
                </>
              )}
              <button
                className="btn btn-primary admin-provision-submit"
                type="submit"
                disabled={busy || loadingAccounts}
              >
                {activeAccountAction === 'provision'
                  ? <LoaderCircle className="admin-loading-icon" size={18} aria-hidden="true" />
                  : mode === 'whitelist'
                    ? <Plus size={18} aria-hidden="true" />
                    : <UserPlus size={18} aria-hidden="true" />}
                {activeAccountAction === 'provision'
                  ? translate('处理中', 'Processing')
                  : mode === 'whitelist'
                    ? translate('加入白名单', 'Add to allowlist')
                    : translate('创建账号', 'Create account')}
              </button>
            </form>

            <section className="admin-account-panel">
              <header className="admin-list-head">
                <div>
                  <h2 ref={accountListHeadingRef} tabIndex={-1}>
                    {translate('授权账号', 'Authorized accounts')}
                  </h2>
                  <small>
                    {translatedTemplate(
                      translate,
                      '显示 {visible} / {total} 个账号',
                      'Showing {visible} of {total} accounts',
                      { visible: filteredAccounts.length, total: accounts.length },
                    )}
                  </small>
                </div>
                <div className="admin-account-tools">
                  <label className="admin-account-search">
                    <Search size={17} aria-hidden="true" />
                    <input
                      type="search"
                      value={accountSearch}
                      placeholder={translate('搜索账号', 'Search accounts')}
                      aria-label={translate('搜索授权账号', 'Search authorized accounts')}
                      onChange={(event) => setAccountSearch(event.target.value)}
                    />
                    {accountSearch && (
                      <button
                        className="admin-search-clear"
                        type="button"
                        onClick={() => setAccountSearch('')}
                        title={translate('清除搜索', 'Clear search')}
                        aria-label={translate('清除账号搜索', 'Clear account search')}
                      >
                        <X size={15} aria-hidden="true" />
                      </button>
                    )}
                  </label>
                  <button
                    className="icon-button"
                    type="button"
                    onClick={() => {
                      setError(null);
                      setMessage(null);
                      void loadAccounts();
                    }}
                    disabled={busy || loadingAccounts}
                    title={translate('刷新账号', 'Refresh accounts')}
                    aria-label={translate('刷新账号', 'Refresh accounts')}
                  >
                    <RefreshCw
                      className={loadingAccounts ? 'admin-loading-icon' : undefined}
                      size={18}
                      aria-hidden="true"
                    />
                  </button>
                </div>
              </header>
              <div
                className="admin-account-filters"
                role="group"
                aria-label={translate('按账号状态筛选', 'Filter by account status')}
              >
                {([
                  ['all', translate('全部', 'All'), accounts.length],
                  ['registered', translate('已注册', 'Registered'), registeredCount],
                  ['enabled', translate('可登录', 'Login enabled'), enabledCount],
                  ['pending', translate('待注册', 'Pending'), pendingCount],
                  ['disabled', translate('已停用', 'Disabled'), disabledCount],
                ] as const).map(([value, label, count]) => (
                  <button
                    key={value}
                    type="button"
                    className={accountFilter === value ? 'is-active' : ''}
                    aria-pressed={accountFilter === value}
                    onClick={() => setAccountFilter(value)}
                  >
                    <span>{label}</span>
                    <small>{count}</small>
                  </button>
                ))}
              </div>
              <div
                className="admin-table"
                role="table"
                aria-label={translate('授权账号列表', 'Authorized account list')}
                aria-busy={loadingAccounts}
              >
                <div className="admin-table-header" role="row">
                  <span role="columnheader">{translate('账号', 'Account')}</span>
                  <span role="columnheader">{translate('身份', 'Status')}</span>
                  <span role="columnheader">{translate('白名单与登录', 'Allowlist and login')}</span>
                  <span role="columnheader">{translate('操作', 'Actions')}</span>
                </div>
                {filteredAccounts.map((account) => {
                  const protectedAdmin = account.role === 'admin';
                  const inactiveRegistered = isInactiveRegisteredAccount(account);
                  const loginEnabled = canAdminAccountLogin(account);
                  const accountStatus = protectedAdmin
                    ? 'admin'
                    : inactiveRegistered
                      ? 'inactive'
                    : account.registered && !inactiveRegistered
                      ? 'registered'
                      : 'pending';
                  const accessStatus = protectedAdmin
                    ? 'protected'
                    : loginEnabled || (!account.registered && account.whitelist_enabled)
                      ? 'enabled'
                      : 'disabled';
                  const toggleBusy = activeAccountAction === `toggle:${account.email}`;
                  const deleteBusy = activeAccountAction === `delete:${account.email}`;
                  const passwordBusy = activeAccountAction === `reset:${account.email}`
                    || activeAccountAction === `restore:${account.email}`;
                  return (
                    <div
                      className={`admin-table-row${toggleBusy || deleteBusy || passwordBusy ? ' is-busy' : ''}`}
                      role="row"
                      key={account.email}
                    >
                      <div className="admin-account-name" role="cell">
                        <strong>{account.display_name || account.email.split('@')[0]}</strong>
                        <span>
                          <Link
                            to={`/admin?section=usage&user=${encodeURIComponent(normalizedAdminEmail(account.email))}`}
                            title={translatedTemplate(
                              translate,
                              '查看 {email} 的使用记录',
                              'View usage records for {email}',
                              { email: account.email },
                            )}
                            aria-label={translatedTemplate(
                              translate,
                              '查看 {email} 的使用记录',
                              'View usage records for {email}',
                              { email: account.email },
                            )}
                          >
                            {account.email}
                          </Link>
                        </span>
                      </div>
                      <span className={`account-role is-${accountStatus}`} role="cell">
                        {protectedAdmin
                          ? translate('管理员', 'Administrator')
                          : inactiveRegistered
                            ? translate('账号已停用', 'Account inactive')
                            : account.registered
                              ? translate('已注册', 'Registered')
                              : translate('待注册', 'Pending registration')}
                      </span>
                      <div className="admin-access-cell" role="cell">
                        <button
                          className="admin-switch"
                          type="button"
                          role="switch"
                          aria-checked={account.whitelist_enabled}
                          aria-label={translatedTemplate(
                            translate,
                            '{email} 的白名单授权',
                            'Allowlist access for {email}',
                            { email: account.email },
                          )}
                          disabled={protectedAdmin || inactiveRegistered || busy || loadingAccounts}
                          onClick={() => void toggleWhitelist(account)}
                        >
                          <span aria-hidden="true" />
                        </button>
                        <span className={`admin-access-state is-${toggleBusy ? 'updating' : accessStatus}`}>
                          {toggleBusy
                            ? translate('更新中', 'Updating')
                            : protectedAdmin
                              ? translate('受保护', 'Protected')
                              : inactiveRegistered
                                ? translate('账号已停用', 'Account inactive')
                                : loginEnabled
                                  ? translate('可登录', 'Login enabled')
                                  : account.whitelist_enabled && !account.registered
                                    ? translate('可注册', 'Registration enabled')
                                    : translate('已停用', 'Disabled')}
                        </span>
                      </div>
                      <div className="admin-row-actions" role="cell">
                        {account.registered && (
                          <button
                            type="button"
                            disabled={busy || loadingAccounts}
                            onClick={() => openPasswordDialog(
                              account,
                              inactiveRegistered ? 'restore' : 'reset',
                            )}
                            title={inactiveRegistered
                              ? translate('恢复账号', 'Restore account')
                              : translate('重置密码', 'Reset password')}
                            aria-label={inactiveRegistered
                              ? translatedTemplate(
                                translate,
                                '恢复 {email} 并设置新密码',
                                'Restore {email} with a new password',
                                { email: account.email },
                              )
                              : translatedTemplate(
                                translate,
                                '重置 {email} 的密码',
                                'Reset the password for {email}',
                                { email: account.email },
                              )}
                          >
                            {passwordBusy
                              ? <LoaderCircle className="admin-loading-icon" size={17} aria-hidden="true" />
                              : <KeyRound size={17} aria-hidden="true" />}
                          </button>
                        )}
                        {!protectedAdmin && (
                          <button
                            className="danger"
                            type="button"
                            disabled={busy || loadingAccounts}
                            onClick={() => {
                              setDeleteTarget(account);
                              setDeleteDialogError(null);
                              setError(null);
                              setMessage(null);
                            }}
                            title={translate('删除授权', 'Remove authorization')}
                            aria-label={translatedTemplate(
                              translate,
                              '删除 {email} 的授权',
                              'Remove authorization for {email}',
                              { email: account.email },
                            )}
                          >
                            {deleteBusy
                              ? <LoaderCircle className="admin-loading-icon" size={17} aria-hidden="true" />
                              : <Trash2 size={17} aria-hidden="true" />}
                          </button>
                        )}
                      </div>
                    </div>
                  );
                })}
                {loadingAccounts && !accounts.length && (
                  <div
                    className="admin-table-skeleton"
                    aria-label={translate('正在读取账号', 'Loading accounts')}
                  >
                    {[0, 1, 2].map((item) => <span key={item} aria-hidden="true" />)}
                  </div>
                )}
                {!loadingAccounts
                  && accountCatalogStatus !== 'error'
                  && !filteredAccounts.length && (
                  <div className="admin-empty">
                    {accountSearch || accountFilter !== 'all'
                      ? translate('没有匹配的账号。', 'No accounts match these filters.')
                      : translate('暂无授权账号。', 'No authorized accounts yet.')}
                  </div>
                )}
              </div>
            </section>
          </section>
        ) : (
          <section
            id="admin-panel-exports"
            className="admin-export-workspace"
            role="tabpanel"
            aria-labelledby="admin-tab-exports"
          >
            <AdminExportPanel busy={busy} />
          </section>
        )}
      </div>

      {passwordDialog && (
        <div
          className="modal-backdrop"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget && !busy) closePasswordDialog();
          }}
        >
          <form
            ref={passwordDialogRef}
            className="admin-reset-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="admin-reset-title"
            tabIndex={-1}
            onSubmit={submitAccountPassword}
          >
            <div className="modal-title-row">
              <h2 id="admin-reset-title">
                {passwordDialog.mode === 'restore'
                  ? translate('恢复账号', 'Restore account')
                  : translate('重置密码', 'Reset password')}
              </h2>
              <button
                className="icon-button"
                type="button"
                onClick={closePasswordDialog}
                disabled={busy}
                title={translate('关闭', 'Close')}
                aria-label={translate('关闭', 'Close')}
              >
                <X size={18} />
              </button>
            </div>
            <p>
              {passwordDialog.account.email}
              {passwordDialog.mode === 'restore' && (
                <><br />{translate(
                  '设置新密码后，白名单和账号将重新启用。',
                  'Setting a new password will re-enable the allowlist entry and account.',
                )}</>
              )}
            </p>
            <label>
              <span>{translate('新密码', 'New password')}</span>
              <div className="password-field">
                <input
                  ref={passwordInputRef}
                  type={resetPasswordVisible ? 'text' : 'password'}
                  value={resetPassword}
                  onChange={(event) => setResetPassword(event.target.value)}
                  autoComplete="new-password"
                  aria-describedby="admin-reset-password-requirement"
                  required
                />
                <button
                  type="button"
                  onClick={() => setResetPasswordVisible((visible) => !visible)}
                  title={resetPasswordVisible
                    ? translate('隐藏密码', 'Hide password')
                    : translate('显示密码', 'Show password')}
                  aria-label={resetPasswordVisible
                    ? translate('隐藏密码', 'Hide password')
                    : translate('显示密码', 'Show password')}
                >
                  {resetPasswordVisible
                    ? <EyeOff size={18} aria-hidden="true" />
                    : <Eye size={18} aria-hidden="true" />}
                </button>
              </div>
              <small id="admin-reset-password-requirement">{translate(
                '至少 8 位数字，或至少 15 位字符',
                'At least 8 digits or at least 15 characters',
              )}</small>
            </label>
            <label>
              <span>{translate('确认新密码', 'Confirm new password')}</span>
              <input
                type={resetPasswordVisible ? 'text' : 'password'}
                value={resetConfirmPassword}
                onChange={(event) => setResetConfirmPassword(event.target.value)}
                autoComplete="new-password"
                aria-invalid={resetConfirmPassword.length > 0
                  && resetPassword !== resetConfirmPassword}
                required
              />
            </label>
            {passwordDialogErrorMessage && (
              <div className="auth-error" role="alert">{passwordDialogErrorMessage}</div>
            )}
            <div className="dialog-actions">
              <button
                className="btn btn-secondary"
                type="button"
                onClick={closePasswordDialog}
                disabled={busy}
              >
                {translate('取消', 'Cancel')}
              </button>
              <button className="btn btn-primary" type="submit" disabled={busy}>
                {activeAccountAction === `${passwordDialog.mode}:${passwordDialog.account.email}`
                  ? <LoaderCircle className="admin-loading-icon" size={17} aria-hidden="true" />
                  : <KeyRound size={17} aria-hidden="true" />}
                {activeAccountAction === `${passwordDialog.mode}:${passwordDialog.account.email}`
                  ? passwordDialog.mode === 'restore'
                    ? translate('正在恢复', 'Restoring')
                    : translate('正在重置', 'Resetting')
                  : passwordDialog.mode === 'restore'
                    ? translate('确认恢复', 'Restore account')
                    : translate('确认重置', 'Reset password')}
              </button>
            </div>
          </form>
        </div>
      )}

      {deleteTarget && (
        <div
          className="modal-backdrop"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget && !busy) closeDeleteDialog();
          }}
        >
          <section
            ref={deleteDialogRef}
            className="admin-confirm-dialog"
            role="alertdialog"
            aria-modal="true"
            aria-labelledby="admin-delete-title"
            aria-describedby="admin-delete-description"
            tabIndex={-1}
          >
            <div className="admin-confirm-dialog-icon is-danger" aria-hidden="true">
              <AlertTriangle size={21} />
            </div>
            <div className="admin-confirm-dialog-copy">
              <h2 id="admin-delete-title">
                {translate('删除账号授权', 'Remove account authorization')}
              </h2>
              <p id="admin-delete-description">
                {translate(
                  '删除后该账号将无法继续登录，历史沟通记录仍会保留。',
                  'This account will no longer be able to sign in. Historical conversation records will be retained.',
                )}
              </p>
              <strong>{deleteTarget.display_name || deleteTarget.email.split('@')[0]}</strong>
              <small>{deleteTarget.email}</small>
              {deleteDialogErrorMessage && (
                <div className="auth-error" role="alert">{deleteDialogErrorMessage}</div>
              )}
            </div>
            <div className="dialog-actions">
              <button
                ref={deleteCancelRef}
                className="btn btn-secondary"
                type="button"
                onClick={closeDeleteDialog}
                disabled={busy}
              >
                {translate('取消', 'Cancel')}
              </button>
              <button
                className="btn admin-danger-button"
                type="button"
                onClick={() => void deleteAccount(deleteTarget)}
                disabled={busy}
              >
                {activeAccountAction === `delete:${deleteTarget.email}`
                  ? <LoaderCircle className="admin-loading-icon" size={17} aria-hidden="true" />
                  : <Trash2 size={17} aria-hidden="true" />}
                {activeAccountAction === `delete:${deleteTarget.email}`
                  ? translate('正在删除', 'Removing')
                  : translate('删除授权', 'Remove authorization')}
              </button>
            </div>
          </section>
        </div>
      )}
    </main>
  );
}

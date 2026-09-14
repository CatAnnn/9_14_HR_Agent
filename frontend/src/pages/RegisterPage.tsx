import { Check, Eye, EyeOff } from 'lucide-react';
import { type FormEvent, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { BoschSupergraphic } from '../components/BoschSupergraphic';
import { AuthBrand } from '../components/AuthBrand';
import { LanguageSelector } from '../components/LanguageSelector';
import { RouteLoading, RouteRedirect } from '../components/RouteLoading';
import { useLanguage } from '../i18n/LanguageContext';
import { useAuthStore } from '../store/authStore';

function validPassword(value: string) {
  return /^\d{8,}$/.test(value) || (!/^\d+$/.test(value) && value.length >= 15);
}

type RegisterErrorCode =
  | 'invalid_password'
  | 'password_mismatch'
  | 'registration_failed';

export default function RegisterPage() {
  const navigate = useNavigate();
  const { initialized, user, register } = useAuthStore();
  const { translate } = useLanguage();
  const [email, setEmail] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [password, setPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [passwordVisible, setPasswordVisible] = useState(false);
  const [registrationSucceeded, setRegistrationSucceeded] = useState(false);
  const [errorCode, setErrorCode] = useState<RegisterErrorCode | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const passwordValid = validPassword(password);
  const passwordsMatch = passwordValid && confirmPassword.length > 0 && password === confirmPassword;
  const error = errorCode === 'invalid_password'
    ? translate('密码需为至少 8 位数字，或至少 15 位字符。', 'Use at least 8 digits, or at least 15 characters.')
    : errorCode === 'password_mismatch'
      ? translate('两次输入的密码不一致。', 'The passwords do not match.')
      : errorCode === 'registration_failed'
        ? translate(
          '注册失败，请使用 @cn.bosch.com 或 @bosch.com 邮箱，并检查填写的信息。',
          'Registration failed. Use an @cn.bosch.com or @bosch.com email address and check the entered information.',
        )
        : '';

  if (!initialized) return <RouteLoading label={translate('正在检查登录状态', 'Checking sign-in status')} />;
  // Guest mode still allows a visitor to create or upgrade an authenticated account.
  if (user && user.role !== 'guest') return <RouteRedirect to="/" />;

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (submitting) return;
    setErrorCode(null);
    setRegistrationSucceeded(false);
    if (!passwordValid) {
      setErrorCode('invalid_password');
      return;
    }
    if (!passwordsMatch) {
      setErrorCode('password_mismatch');
      return;
    }
    setSubmitting(true);
    try {
      const ok = await register(email, password, displayName || undefined);
      if (!ok) {
        setErrorCode('registration_failed');
        return;
      }
      setRegistrationSucceeded(true);
      window.setTimeout(() => navigate('/login', { replace: true }), 700);
    } catch {
      setErrorCode('registration_failed');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <main className="auth-page auth-register-page">
      <BoschSupergraphic />
      <LanguageSelector className="auth-language-toggle" compact />
      <section className="auth-surface auth-surface-wide auth-register-surface" aria-labelledby="register-title">
        <div className="auth-brand-row">
          <AuthBrand />
        </div>
        <div className="auth-heading">
          <h1 id="register-title">{translate('创建账号', 'Create account')}</h1>
          <p>{translate('请使用 @cn.bosch.com 或 @bosch.com 邮箱注册。', 'Register with an @cn.bosch.com or @bosch.com email address.')}</p>
        </div>
        <form className="auth-form auth-form-grid" onSubmit={submit}>
          <label>
            <span>{translate('邮箱', 'Email')}</span>
            <input
              type="email"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              autoComplete="email"
              autoFocus
              required
            />
          </label>
          <label>
            <span>{translate('显示名称', 'Display name')}</span>
            <input
              value={displayName}
              onChange={(event) => setDisplayName(event.target.value)}
              autoComplete="name"
            />
          </label>
          <label>
            <span>{translate('密码', 'Password')}</span>
            <div className="password-field">
              <input
                type={passwordVisible ? 'text' : 'password'}
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                autoComplete="new-password"
                aria-describedby="password-requirement"
                required
              />
              {passwordValid ? (
                <Check className="field-valid" size={18} aria-label={translate('密码格式正确', 'Password format is valid')} />
              ) : (
                <button
                  type="button"
                  onClick={() => setPasswordVisible((value) => !value)}
                  title={passwordVisible ? translate('隐藏密码', 'Hide password') : translate('显示密码', 'Show password')}
                  aria-label={passwordVisible ? translate('隐藏密码', 'Hide password') : translate('显示密码', 'Show password')}
                >
                  {passwordVisible ? <EyeOff size={18} /> : <Eye size={18} />}
                </button>
              )}
            </div>
            <small id="password-requirement">{translate('至少 8 位数字，或至少 15 位字符', 'At least 8 digits, or at least 15 characters')}</small>
          </label>
          <label>
            <span>{translate('确认密码', 'Confirm password')}</span>
            <div className="password-field">
              <input
                type={passwordVisible ? 'text' : 'password'}
                value={confirmPassword}
                onChange={(event) => setConfirmPassword(event.target.value)}
                autoComplete="new-password"
                aria-invalid={confirmPassword.length > 0 && !passwordsMatch}
                required
              />
              {passwordsMatch && <Check className="field-valid" size={18} aria-label={translate('两次密码一致', 'Passwords match')} />}
            </div>
          </label>
          {error && <div className="auth-error auth-form-message" role="alert">{error}</div>}
          {registrationSucceeded && (
            <div className="auth-success auth-form-message" role="status">
              {translate(
                '注册成功，正在返回登录页。',
                'Registration succeeded. Returning to sign in.',
              )}
            </div>
          )}
          <button className="btn btn-primary btn-large auth-form-submit" type="submit" disabled={submitting}>
            {submitting ? translate('正在创建', 'Creating account') : translate('创建账号', 'Create account')}
          </button>
        </form>
        <footer className="auth-switch">
          {translate('已有账号？', 'Already have an account? ')}<Link to="/login">{translate('返回登录', 'Back to sign in')}</Link>
        </footer>
      </section>
    </main>
  );
}

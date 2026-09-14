import { Eye, EyeOff } from 'lucide-react';
import { type FormEvent, useState } from 'react';
import { Link, useLocation, useNavigate } from 'react-router-dom';
import { BoschSupergraphic } from '../components/BoschSupergraphic';
import { AuthBrand } from '../components/AuthBrand';
import { LanguageSelector } from '../components/LanguageSelector';
import { RouteLoading, RouteRedirect } from '../components/RouteLoading';
import { useLanguage } from '../i18n/LanguageContext';
import { useAuthStore } from '../store/authStore';

export default function LoginPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const { initialized, user, login } = useAuthStore();
  const { translate } = useLanguage();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [passwordVisible, setPasswordVisible] = useState(false);
  const [loginFailed, setLoginFailed] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const from = (location.state as { from?: string } | null)?.from || '/';

  if (!initialized) return <RouteLoading label={translate('正在检查登录状态', 'Checking sign-in status')} />;
  // Guest mode still allows a visitor to upgrade to an authenticated account.
  if (user && user.role !== 'guest') return <RouteRedirect to="/" />;

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (submitting) return;
    setLoginFailed(false);
    setSubmitting(true);
    try {
      await login(email, password);
      navigate(from, { replace: true });
    } catch {
      setLoginFailed(true);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <main className="auth-page auth-login-page">
      <BoschSupergraphic />
      <LanguageSelector className="auth-language-toggle" compact />
      <section className="auth-surface auth-login-surface" aria-labelledby="login-title">
        <div className="auth-login-header">
          <div className="auth-brand-row">
            <AuthBrand />
          </div>
          <div className="auth-heading">
            <h1 id="login-title">{translate('登录', 'Sign in')}</h1>
            <p>{translate('使用 @cn.bosch.com 或 @bosch.com 邮箱登录', 'Sign in with an @cn.bosch.com or @bosch.com email address.')}</p>
          </div>
        </div>
        <form className="auth-form" onSubmit={submit}>
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
            <span>{translate('密码', 'Password')}</span>
            <div className="password-field">
              <input
                type={passwordVisible ? 'text' : 'password'}
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                autoComplete="current-password"
                required
              />
              <button
                type="button"
                onClick={() => setPasswordVisible((value) => !value)}
                title={passwordVisible ? translate('隐藏密码', 'Hide password') : translate('显示密码', 'Show password')}
                aria-label={passwordVisible ? translate('隐藏密码', 'Hide password') : translate('显示密码', 'Show password')}
              >
                {passwordVisible ? <EyeOff size={18} /> : <Eye size={18} />}
              </button>
            </div>
          </label>
          {loginFailed && (
            <div className="auth-error" role="alert">
              {translate(
                '邮箱或密码错误，或账号暂不可用。',
                'The email or password is incorrect, or this account is currently unavailable.',
              )}
            </div>
          )}
          <button className="btn btn-primary btn-large" type="submit" disabled={submitting}>
            {submitting ? translate('正在登录', 'Signing in') : translate('登录', 'Sign in')}
          </button>
        </form>
        <footer className="auth-switch">
          {translate('首次使用？', 'New here? ')}<Link to="/register">{translate('注册账号', 'Create an account')}</Link>
        </footer>
      </section>
    </main>
  );
}

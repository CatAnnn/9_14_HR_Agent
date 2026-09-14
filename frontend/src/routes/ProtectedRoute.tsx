import { Outlet, useLocation } from 'react-router-dom';
import { RouteLoading, RouteRedirect } from '../components/RouteLoading';
import { useLanguage } from '../i18n/LanguageContext';
import { useAuthStore } from '../store/authStore';

export function ProtectedRoute() {
  const location = useLocation();
  const { initialized, user } = useAuthStore();
  const { translate } = useLanguage();

  if (!initialized) {
    return <RouteLoading label={translate('正在检查登录状态', 'Checking sign-in status')} />;
  }
  if (!user) {
    return (
      <RouteRedirect
        to="/login"
        state={{ from: `${location.pathname}${location.search}` }}
      />
    );
  }
  return <Outlet />;
}

import { Navigate } from 'react-router-dom';
import { useLanguage } from '../i18n/LanguageContext';

interface RouteLoadingProps {
  compact?: boolean;
  label?: string;
}

interface RouteRedirectProps {
  replace?: boolean;
  state?: unknown;
  to: string;
}

export function RouteLoading({ compact = false, label }: RouteLoadingProps) {
  const { translate } = useLanguage();
  const displayLabel = label || translate('正在加载', 'Loading');
  const content = (
    <>
      <span className="route-loading-track" aria-hidden="true" />
      <span className="route-loading-label">{displayLabel}</span>
    </>
  );

  if (compact) {
    return (
      <div className="step-loading" role="status" aria-busy="true" aria-live="polite">
        {content}
      </div>
    );
  }

  return (
    <main className="route-loading" role="status" aria-busy="true" aria-live="polite">
      {content}
    </main>
  );
}

export function RouteRedirect({
  replace = true,
  state,
  to,
}: RouteRedirectProps) {
  return <Navigate to={to} replace={replace} state={state} />;
}

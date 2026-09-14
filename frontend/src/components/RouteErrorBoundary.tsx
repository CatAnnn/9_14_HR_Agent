import {
  Component,
  type ErrorInfo,
  type ReactNode,
} from 'react';
import { useLanguage } from '../i18n/LanguageContext';

interface RouteErrorBoundaryProps {
  children: ReactNode;
  resetKey: string;
  translate?: (source: string, english?: string) => string;
}

interface RouteErrorBoundaryState {
  error: unknown;
  resetKey: string;
}

export class RouteErrorBoundary extends Component<
  RouteErrorBoundaryProps,
  RouteErrorBoundaryState
> {
  state: RouteErrorBoundaryState = {
    error: null,
    resetKey: this.props.resetKey,
  };

  static getDerivedStateFromError(error: unknown): Partial<RouteErrorBoundaryState> {
    return { error };
  }

  static getDerivedStateFromProps(
    props: RouteErrorBoundaryProps,
    state: RouteErrorBoundaryState,
  ): Partial<RouteErrorBoundaryState> | null {
    if (props.resetKey === state.resetKey) return null;
    return {
      error: null,
      resetKey: props.resetKey,
    };
  }

  componentDidCatch(error: unknown, info: ErrorInfo): void {
    console.error('route_render_failed', error, info.componentStack);
  }

  render() {
    if (!this.state.error) return this.props.children;

    return (
      <main className="route-error" role="alert" aria-live="assertive">
        <div>
          <h1>{this.props.translate?.('页面未能载入', 'The page could not be loaded') || '页面未能载入'}</h1>
          <p>{this.props.translate?.('页面资源加载中断，请重新载入后继续。', 'Page resources were interrupted. Reload to continue.') || '页面资源加载中断，请重新载入后继续。'}</p>
          <button
            className="btn btn-primary"
            type="button"
            onClick={() => window.location.reload()}
          >
            {this.props.translate?.('重新载入', 'Reload') || '重新载入'}
          </button>
        </div>
      </main>
    );
  }
}

export function LocalizedRouteErrorBoundary(
  props: Omit<RouteErrorBoundaryProps, 'translate'>,
) {
  const { translate } = useLanguage();
  return <RouteErrorBoundary {...props} translate={translate} />;
}

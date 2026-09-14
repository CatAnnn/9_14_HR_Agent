import {
  Suspense,
  useEffect,
  useRef,
  useState,
} from 'react';
import { Route, Routes, useLocation } from 'react-router-dom';
import {
  RESOURCE_LEGACY_PATHS,
  RESOURCE_PATHS,
  isLegacyResourceHash,
} from './config/resourceRoutes';
import { STATIC_ASSETS } from './config/staticAssets';
import { LocalizedRouteErrorBoundary } from './components/RouteErrorBoundary';
import { RouteLoading, RouteRedirect } from './components/RouteLoading';
import { ScrollManager } from './components/ScrollManager';
import { useLanguage } from './i18n/LanguageContext';
import { ProtectedRoute } from './routes/ProtectedRoute';
import { lazyRoute, type PreloadableRoute } from './utils/lazyRoute';
import { scheduleIdlePrefetch, shouldPrefetch } from './utils/prefetch';
import { routeDocumentMetadata } from './utils/routeDocumentMetadata';

function preloadHomeHero(): void {
  if (document.head.querySelector('[data-home-hero-preload]')) return;

  const link = document.createElement('link');
  link.rel = 'preload';
  link.as = 'image';
  link.href = window.matchMedia('(max-width: 680px)').matches
    ? STATIC_ASSETS.homeHeroMobile
    : STATIC_ASSETS.homeHeroDesktop;
  link.dataset.homeHeroPreload = '';
  link.setAttribute('fetchpriority', 'high');
  document.head.append(link);
}

const AdminPage = lazyRoute(() => import('./pages/AdminPage'));
const AdminUsageSessionPage = lazyRoute(() => import('./pages/AdminUsageSessionPage'));
const AdminTestRunnerPage = lazyRoute(() => import('./pages/AdminTestRunnerPage'));
const AdminTestWorkflowPage = lazyRoute(() => import('./pages/AdminTestWorkflowPage'));
const AdminTestWorkflowLayout = lazyRoute(() => import('./routes/AdminTestWorkflowLayout'));
const AgentIntroductionPage = lazyRoute(() => import('./pages/AgentIntroductionRoute'));
const HomePage = lazyRoute(() => {
  preloadHomeHero();
  return import('./pages/HomePage');
});
const EbookDetailPage = lazyRoute(() => import('./pages/EbookDetailPage'));
const EbookReaderPage = lazyRoute(() => import('./pages/EbookReaderPage'));
const LoginPage = lazyRoute(() => import('./pages/LoginPage'));
const PerformanceManagementDetailPage = lazyRoute(() => import('./pages/PerformanceManagementDetailPage'));
const RegisterPage = lazyRoute(() => import('./pages/RegisterPage'));
const ResourcePage = lazyRoute(() => import('./pages/ResourcePage'));
const SolutionsPage = lazyRoute(() => import('./pages/SolutionsPage'));
const ToolkitPage = lazyRoute(() => import('./pages/ToolkitPage'));
const WorkspacePage = lazyRoute(() => import('./pages/WorkspacePage'));
const WorkflowLayout = lazyRoute(() => import('./routes/WorkflowLayout'));

const LEGACY_RESOURCE_PATHS = new Set<string>(RESOURCE_LEGACY_PATHS);

function preloadRoutes(...routes: PreloadableRoute[]): Promise<void> {
  return Promise.all(routes.map((route) => route.preload())).then(() => undefined);
}

function preloadPath(pathname: string): Promise<void> {
  if (pathname === '/login') return LoginPage.preload();
  if (pathname === '/register') return RegisterPage.preload();
  if (/^\/resource\/book\/[^/]+\/read\/?$/u.test(pathname)) {
    return EbookReaderPage.preload();
  }
  if (/^\/resource\/book\/[^/]+\/?$/u.test(pathname)) {
    return EbookDetailPage.preload();
  }
  if (pathname.startsWith('/admin/test-workflow/')) {
    return preloadRoutes(AdminTestWorkflowLayout, AdminTestRunnerPage);
  }
  if (pathname === '/admin/test-workflow') return AdminTestWorkflowPage.preload();
  if (pathname.startsWith('/admin/usage/')) return AdminUsageSessionPage.preload();
  if (pathname === '/admin') return AdminPage.preload();
  if (pathname === RESOURCE_PATHS.home || LEGACY_RESOURCE_PATHS.has(pathname)) {
    return ResourcePage.preload();
  }
  if (pathname.startsWith(RESOURCE_PATHS.performanceManagement + '/')) {
    return PerformanceManagementDetailPage.preload();
  }
  if (pathname === '/solutions') {
    return preloadRoutes(WorkflowLayout, SolutionsPage);
  }
  if (pathname.startsWith('/solutions/')) {
    return preloadRoutes(WorkflowLayout, ToolkitPage);
  }
  if (pathname === '/app/introduction') {
    return preloadRoutes(WorkflowLayout, AgentIntroductionPage);
  }
  if (pathname === '/app' || pathname.startsWith('/app/')) {
    return preloadRoutes(WorkflowLayout, WorkspacePage);
  }
  if (pathname === '/') return preloadRoutes(WorkflowLayout, HomePage);
  return Promise.resolve();
}

function RoutePrefetcher() {
  useEffect(() => {
    let hoveredAnchor: HTMLAnchorElement | null = null;
    let cancelHoverPrefetch: (() => void) | undefined;
    const cancelHover = () => {
      cancelHoverPrefetch?.();
      cancelHoverPrefetch = undefined;
      hoveredAnchor = null;
    };

    const preloadLinkedRoute = (event: Event) => {
      const target = event.target;
      if (!(target instanceof Element)) return;
      const anchor = target.closest<HTMLAnchorElement>('a[href]');
      if (!anchor || anchor.hasAttribute('download') || !shouldPrefetch()) return;
      if (anchor.target && anchor.target !== '_self') return;

      const url = new URL(anchor.href, window.location.href);
      if (url.origin !== window.location.origin || url.pathname === window.location.pathname) return;
      const preload = () => preloadPath(url.pathname);
      if (event.type === 'pointerover') {
        if (hoveredAnchor === anchor) return;
        cancelHover();
        hoveredAnchor = anchor;
        // Crossing a menu should not download every page under the pointer.
        cancelHoverPrefetch = scheduleIdlePrefetch(preload, { delayMs: 120, idleTimeoutMs: 300 });
      } else {
        cancelHover();
        void preload().catch(() => undefined);
      }
    };

    const cancelOnPointerLeave = (event: PointerEvent) => {
      if (!hoveredAnchor) return;
      if (event.relatedTarget instanceof Node && hoveredAnchor.contains(event.relatedTarget)) return;
      cancelHover();
    };

    document.addEventListener('pointerover', preloadLinkedRoute, true);
    document.addEventListener('pointerout', cancelOnPointerLeave, true);
    document.addEventListener('focusin', preloadLinkedRoute, true);
    document.addEventListener('touchstart', preloadLinkedRoute, {
      capture: true,
      passive: true,
    });

    return () => {
      cancelHover();
      document.removeEventListener('pointerover', preloadLinkedRoute, true);
      document.removeEventListener('pointerout', cancelOnPointerLeave, true);
      document.removeEventListener('focusin', preloadLinkedRoute, true);
      document.removeEventListener('touchstart', preloadLinkedRoute, true);
    };
  }, []);

  return null;
}

function RouteTransitionController() {
  const location = useLocation();
  const previousKeyRef = useRef(location.key);
  const [transitionKey, setTransitionKey] = useState<string | null>(null);

  useEffect(() => {
    if (previousKeyRef.current === location.key) return undefined;
    previousKeyRef.current = location.key;

    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      setTransitionKey(null);
      return undefined;
    }

    const activeKey = location.key;
    setTransitionKey(activeKey);
    const timer = window.setTimeout(() => {
      setTransitionKey((current) => current === activeKey ? null : current);
    }, 220);

    return () => {
      window.clearTimeout(timer);
    };
  }, [location.key]);

  return transitionKey
    ? <span key={transitionKey} className="route-settling-indicator" aria-hidden="true" />
    : null;
}

function RouteDocumentMetadata() {
  const location = useLocation();
  const { language, translate } = useLanguage();

  useEffect(() => {
    document.documentElement.lang = language;
    document.documentElement.dataset.language = language;
    const metadata = routeDocumentMetadata(location.pathname);
    if (metadata) document.title = translate(metadata.title, metadata.titleEnglish);
  }, [language, location.pathname, translate]);

  return null;
}

function ResourceHomeRoute() {
  const { hash } = useLocation();

  if (isLegacyResourceHash(hash)) {
    return <RouteRedirect to={`${RESOURCE_PATHS.home}#performance-management-dimensions`} />;
  }

  return <ResourcePage />;
}

export default function App() {
  const location = useLocation();

  return (
    <LocalizedRouteErrorBoundary resetKey={location.key}>
      <RouteDocumentMetadata />
      <RoutePrefetcher />
      <RouteTransitionController />
      <Suspense fallback={<RouteLoading />}>
        <ScrollManager />
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route path="/register" element={<RegisterPage />} />
          <Route path="/resource/book/:bookId" element={<EbookDetailPage />} />
          <Route path={RESOURCE_PATHS.home} element={<ResourceHomeRoute />} />
          {RESOURCE_LEGACY_PATHS.map((path) => (
            <Route key={path} path={path} element={<RouteRedirect to={RESOURCE_PATHS.home} />} />
          ))}
          <Route element={<ProtectedRoute />}>
            <Route
              path={RESOURCE_PATHS.performanceManagementDimension}
              element={<PerformanceManagementDetailPage />}
            />
            <Route path="/resource/book/:bookId/read" element={<EbookReaderPage />} />
            <Route path="/admin" element={<AdminPage />} />
            <Route path="/admin/usage/:sessionId/:section?" element={<AdminUsageSessionPage />} />
            <Route path="/admin/test-workflow" element={<AdminTestWorkflowPage />} />
            <Route element={<AdminTestWorkflowLayout />}>
              <Route path="/admin/test-workflow/:step" element={<AdminTestRunnerPage />} />
            </Route>
            <Route element={<WorkflowLayout />}>
              <Route path="/" element={<HomePage />} />
              <Route path="/solutions" element={<SolutionsPage />} />
              <Route path="/solutions/:toolkitSlug/:toolSlug" element={<ToolkitPage />} />
              <Route path="/solutions/:toolkitSlug" element={<ToolkitPage />} />
              <Route path="/app/introduction" element={<AgentIntroductionPage />} />
              <Route path="/app" element={<WorkspacePage />} />
              <Route path="/app/:step" element={<WorkspacePage />} />
            </Route>
          </Route>
          <Route path="*" element={<RouteRedirect to="/" />} />
        </Routes>
      </Suspense>
    </LocalizedRouteErrorBoundary>
  );
}

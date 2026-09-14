import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
  type TransitionEvent as ReactTransitionEvent,
} from 'react';
import { createPortal } from 'react-dom';
import { useLocation } from 'react-router-dom';
import {
  agentIntroductionContent,
  localizeIntroductionText,
  type AgentIntroductionGuide,
  type LocalizedIntroductionText,
} from '../content/agent-introduction-content';
import { useLanguage } from '../i18n/LanguageContext';
import { useAuthStore } from '../store/authStore';
import type { StepKey } from '../types/domain';
import {
  dismissWorkflowGuidePage,
  emptyWorkflowGuideState,
  readWorkflowGuideState,
  resetWorkflowGuidePage,
  writeWorkflowGuideState,
  type WorkflowGuidePageKey,
  type WorkflowGuideStorage,
  type WorkflowGuideState,
} from '../utils/workflowGuideStorage';
import {
  isWorkflowPageGuideAlwaysShow,
  isWorkflowPageGuideEnabled,
} from '../utils/runtimeConfig';
import { calculateWorkflowGuideSpotlight } from '../utils/workflowGuideGeometry';

interface WorkflowPageGuideContextValue {
  restartCurrentPage: () => void;
  available: boolean;
}

const WorkflowPageGuideContext = createContext<WorkflowPageGuideContextValue | null>(null);
const DISABLED_WORKFLOW_PAGE_GUIDE: WorkflowPageGuideContextValue = {
  restartCurrentPage: () => undefined,
  available: false,
};

const GUIDE_DELAY_MS = 180;
const GUIDE_EXIT_FALLBACK_MS = 360;
const SPOTLIGHT_FALLBACK_PADDING_PX = 16;

interface WorkflowGuideSurface {
  page: StepKey;
  key: WorkflowGuidePageKey;
  hostSelector: string;
  headingSelector: 'h1' | 'h3';
}

interface SpotlightRect {
  key: WorkflowGuidePageKey;
  visitKey: string;
  top: number;
  left: number;
  width: number;
  height: number;
}

function routeStep(pathname: string): StepKey | null {
  const step = pathname.match(/^\/app\/(profile|intent|simulation|guidance|rehearsal|report)(?:\/|$)/)?.[1];
  return step ? step as StepKey : null;
}

function routeGuideSurface(pathname: string, search: string): WorkflowGuideSurface | null {
  const page = routeStep(pathname);
  if (!page) return null;
  if (page === 'intent' && new URLSearchParams(search).get('stage') === 'performance') {
    return {
      page,
      key: 'intent-performance',
      hostSelector: '#screen-intent > .page-intro',
      headingSelector: 'h1',
    };
  }
  return {
    page,
    key: page,
    hostSelector: `#screen-${page} > .page-intro`,
    headingSelector: 'h1',
  };
}

function findNextContentBoundary(titleHost: HTMLElement) {
  const boundary = titleHost.nextElementSibling;
  return boundary instanceof HTMLElement ? boundary : null;
}

function browserGuideStorage(alwaysShow: boolean): WorkflowGuideStorage | null {
  try {
    return alwaysShow ? window.sessionStorage : window.localStorage;
  } catch {
    return null;
  }
}

function EnabledWorkflowPageGuideProvider({ children }: { children: ReactNode }) {
  const location = useLocation();
  const { user } = useAuthStore();
  const { language, translate } = useLanguage();
  const alwaysShow = isWorkflowPageGuideAlwaysShow();
  const surface = useMemo(
    () => routeGuideSurface(location.pathname, location.search),
    [location.pathname, location.search],
  );
  const currentPage = surface?.page || null;
  const guidePageKey = surface?.key || null;
  const hostSelector = surface?.hostSelector || '';
  const userKey = user?.id || user?.email || '';
  const guideScopeKey = `${alwaysShow ? 'session' : 'local'}:${userKey}`;
  const [guideState, setGuideState] = useState<WorkflowGuideState>(emptyWorkflowGuideState);
  const [loadedGuideScopeKey, setLoadedGuideScopeKey] = useState('');
  const [activationToken, setActivationToken] = useState(0);
  const [readyVisitKey, setReadyVisitKey] = useState('');
  const [exitingVisitKey, setExitingVisitKey] = useState('');
  const [enteredVisitKey, setEnteredVisitKey] = useState('');
  const [titleHost, setTitleHost] = useState<HTMLElement | null>(null);
  const [spotlightRect, setSpotlightRect] = useState<SpotlightRect | null>(null);
  const guideRef = useRef<HTMLElement | null>(null);
  const previousGuidePageRef = useRef<{
    userKey: string;
    pageKey: WorkflowGuidePageKey;
  } | null>(null);
  const enterFrameRef = useRef<number | null>(null);
  const exitTimerRef = useRef<number | null>(null);

  const clearEnterFrame = useCallback(() => {
    if (enterFrameRef.current === null) return;
    window.cancelAnimationFrame(enterFrameRef.current);
    enterFrameRef.current = null;
  }, []);

  const clearExitTimer = useCallback(() => {
    if (exitTimerRef.current === null) return;
    window.clearTimeout(exitTimerRef.current);
    exitTimerRef.current = null;
  }, []);

  useEffect(() => {
    if (!userKey) {
      setGuideState(emptyWorkflowGuideState());
      setLoadedGuideScopeKey('');
      previousGuidePageRef.current = null;
      return;
    }
    const storage = browserGuideStorage(alwaysShow);
    setGuideState(storage
      ? readWorkflowGuideState(storage, userKey)
      : emptyWorkflowGuideState());
    setLoadedGuideScopeKey(guideScopeKey);
    previousGuidePageRef.current = null;
  }, [alwaysShow, guideScopeKey, userKey]);

  const persist = useCallback((update: (current: WorkflowGuideState) => WorkflowGuideState) => {
    setGuideState((current) => {
      const next = update(current);
      const storage = browserGuideStorage(alwaysShow);
      if (userKey && storage) writeWorkflowGuideState(storage, userKey, next);
      return next;
    });
  }, [alwaysShow, userKey]);

  const introduction = currentPage
    ? agentIntroductionContent.steps.find(({ id }) => id === currentPage) || null
    : null;
  const guideCopy: AgentIntroductionGuide | null = introduction
    ? guidePageKey === 'intent-performance'
      ? introduction.performanceGuide || null
      : introduction.guide
    : null;

  useLayoutEffect(() => {
    setTitleHost(null);
    if (!hostSelector) return undefined;

    const resolveTitleHost = () => {
      const host = document.querySelector<HTMLElement>(hostSelector);
      if (!host) return false;
      setTitleHost(host);
      return true;
    };

    if (resolveTitleHost()) return undefined;

    const observer = new MutationObserver(() => {
      if (resolveTitleHost()) observer.disconnect();
    });
    observer.observe(document.body, { childList: true, subtree: true });
    return () => observer.disconnect();
  }, [hostSelector, location.key]);

  const visitKey = guidePageKey ? `${userKey}:${location.key}:${guidePageKey}` : '';
  const isExiting = Boolean(visitKey && exitingVisitKey === visitKey);
  const guideDismissed = Boolean(guidePageKey && guideState.pages[guidePageKey]?.dismissed);
  const isEntered = Boolean(visitKey && enteredVisitKey === visitKey);

  useEffect(() => {
    const previous = previousGuidePageRef.current;
    if (
      previous
      && previous.userKey === userKey
      && previous.pageKey !== guidePageKey
    ) {
      persist((current) => dismissWorkflowGuidePage(current, previous.pageKey));
    }
    previousGuidePageRef.current = userKey && guidePageKey
      ? { userKey, pageKey: guidePageKey }
      : null;
  }, [guidePageKey, persist, userKey]);

  useEffect(() => {
    setReadyVisitKey('');
    if (
      !guidePageKey
      || !guideCopy
      || !userKey
      || loadedGuideScopeKey !== guideScopeKey
      || guideDismissed
    ) {
      return undefined;
    }
    const timer = window.setTimeout(() => setReadyVisitKey(visitKey), GUIDE_DELAY_MS);
    return () => window.clearTimeout(timer);
  }, [
    activationToken,
    guideCopy,
    guideDismissed,
    guidePageKey,
    guideScopeKey,
    loadedGuideScopeKey,
    userKey,
    visitKey,
  ]);

  const finishExit = useCallback(() => {
    clearExitTimer();
    setEnteredVisitKey('');
    setExitingVisitKey('');
  }, [clearExitTimer]);

  useEffect(() => {
    clearEnterFrame();
    clearExitTimer();
    setEnteredVisitKey('');
    setExitingVisitKey('');
    return () => {
      clearEnterFrame();
      clearExitTimer();
    };
  }, [clearEnterFrame, clearExitTimer, visitKey]);

  useEffect(() => {
    if (!isExiting) return undefined;
    clearExitTimer();
    const reducedMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)');
    if (reducedMotion?.matches) {
      finishExit();
      return undefined;
    }
    const handleReducedMotion = (event: MediaQueryListEvent) => {
      if (event.matches) finishExit();
    };
    reducedMotion?.addEventListener('change', handleReducedMotion);
    exitTimerRef.current = window.setTimeout(finishExit, GUIDE_EXIT_FALLBACK_MS);
    return () => {
      reducedMotion?.removeEventListener('change', handleReducedMotion);
      clearExitTimer();
    };
  }, [clearExitTimer, finishExit, isExiting]);

  const dismiss = useCallback(() => {
    if (!guidePageKey || isExiting) return;
    const reducedMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false;
    if (!reducedMotion && isEntered) setExitingVisitKey(visitKey);
    setReadyVisitKey('');
    persist((current) => dismissWorkflowGuidePage(current, guidePageKey));
    if (reducedMotion || !isEntered) {
      finishExit();
    }
  }, [finishExit, guidePageKey, isEntered, isExiting, persist, visitKey]);

  const restartCurrentPage = useCallback(() => {
    if (!guidePageKey) return;
    clearEnterFrame();
    clearExitTimer();
    setEnteredVisitKey('');
    setExitingVisitKey('');
    setReadyVisitKey('');
    persist((current) => resetWorkflowGuidePage(current, guidePageKey));
    setActivationToken((value) => value + 1);
  }, [clearEnterFrame, clearExitTimer, guidePageKey, persist]);

  const visible = Boolean(
    guideCopy
    && guidePageKey
    && readyVisitKey === visitKey
    && !guideDismissed,
  );
  const presented = visible || isExiting;
  const isActive = isEntered && !isExiting;

  useLayoutEffect(() => {
    if (!surface || !guideCopy || !titleHost) return undefined;
    titleHost.classList.add('workflow-page-guide-host');
    return () => titleHost.classList.remove('workflow-page-guide-host');
  }, [guideCopy, surface, titleHost]);

  useLayoutEffect(() => {
    if (!presented || !surface || !titleHost) {
      setSpotlightRect(null);
      return undefined;
    }

    const heading = titleHost.querySelector<HTMLElement>(`:scope > ${surface.headingSelector}`);
    const guideElement = guideRef.current;
    if (!heading || !guideElement) {
      setSpotlightRect(null);
      return undefined;
    }

    let frame = 0;
    let active = true;
    const contentBoundary = findNextContentBoundary(titleHost);

    const updateSpotlight = () => {
      if (!active) return;
      const headingRect = heading.getBoundingClientRect();
      const guideRect = guideElement.getBoundingClientRect();
      const boundaryRect = contentBoundary?.getBoundingClientRect();
      const viewportWidth = document.documentElement.clientWidth || window.innerWidth;
      const viewportHeight = document.documentElement.clientHeight || window.innerHeight;
      const geometry = calculateWorkflowGuideSpotlight({
        viewportWidth,
        viewportHeight,
        focusBottom: Math.max(headingRect.bottom, guideRect.bottom)
          + SPOTLIGHT_FALLBACK_PADDING_PX,
        nextModuleTop: boundaryRect?.top,
      });
      const next: SpotlightRect = {
        key: surface.key,
        visitKey,
        ...geometry,
      };
      setSpotlightRect((current) => (
        current
        && current.key === next.key
        && current.visitKey === next.visitKey
        && current.top === next.top
        && current.left === next.left
        && current.width === next.width
        && current.height === next.height
          ? current
          : next
      ));
    };
    const scheduleSpotlightUpdate = () => {
      if (!active || frame) return;
      frame = window.requestAnimationFrame(() => {
        frame = 0;
        updateSpotlight();
      });
    };

    const resizeObserver = typeof ResizeObserver === 'undefined'
      ? null
      : new ResizeObserver(scheduleSpotlightUpdate);
    resizeObserver?.observe(heading);
    resizeObserver?.observe(titleHost);
    if (contentBoundary) resizeObserver?.observe(contentBoundary);
    updateSpotlight();

    const mutationRoot = titleHost.parentElement;
    const mutationObserver = mutationRoot
      ? new MutationObserver(scheduleSpotlightUpdate)
      : null;
    if (mutationRoot) mutationObserver?.observe(mutationRoot, { childList: true });

    void document.fonts?.ready.then(() => {
      if (active) scheduleSpotlightUpdate();
    });
    window.addEventListener('resize', scheduleSpotlightUpdate);
    window.addEventListener('scroll', scheduleSpotlightUpdate, { passive: true });
    return () => {
      active = false;
      if (frame) window.cancelAnimationFrame(frame);
      resizeObserver?.disconnect();
      mutationObserver?.disconnect();
      window.removeEventListener('resize', scheduleSpotlightUpdate);
      window.removeEventListener('scroll', scheduleSpotlightUpdate);
    };
  }, [language, presented, surface, titleHost, visitKey]);

  const spotlightReady = Boolean(
    visible
    && guidePageKey
    && spotlightRect?.key === guidePageKey
    && spotlightRect.visitKey === visitKey,
  );

  useEffect(() => {
    clearEnterFrame();
    if (!spotlightReady || isExiting) {
      if (!isExiting) setEnteredVisitKey('');
      return clearEnterFrame;
    }

    enterFrameRef.current = window.requestAnimationFrame(() => {
      enterFrameRef.current = null;
      setEnteredVisitKey(visitKey);
    });
    return clearEnterFrame;
  }, [clearEnterFrame, isExiting, spotlightReady, visitKey]);

  const handleSpotlightTransitionEnd = useCallback((
    event: ReactTransitionEvent<HTMLDivElement>,
  ) => {
    if (
      !isExiting
      || event.target !== event.currentTarget
      || event.propertyName !== 'opacity'
    ) return;
    finishExit();
  }, [finishExit, isExiting]);

  useEffect(() => {
    if (!presented) return undefined;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') dismiss();
    };
    document.addEventListener('keydown', handleKeyDown);
    return () => {
      document.removeEventListener('keydown', handleKeyDown);
    };
  }, [dismiss, presented]);

  const contextValue = useMemo<WorkflowPageGuideContextValue>(() => ({
    restartCurrentPage,
    available: Boolean(guidePageKey && guideCopy),
  }), [guideCopy, guidePageKey, restartCurrentPage]);

  const localize = (value: LocalizedIntroductionText) => (
    localizeIntroductionText(value, language)
  );
  const guideTitle = guidePageKey === 'intent-performance'
    ? translate('员工表现', 'Employee performance')
    : introduction ? localize(introduction.title) : '';
  const guide = guideCopy && guidePageKey && titleHost
    ? createPortal(
      <aside
        ref={guideRef}
        className={`workflow-page-guide${presented ? ' is-presented' : ' is-idle'}${isActive ? ' is-active' : ''}${isExiting ? ' is-exiting' : ''}`}
        role="note"
        aria-hidden={!isActive}
        aria-label={`${guideTitle}: ${localize(guideCopy.task)} ${localize(guideCopy.meaning)}`}
      >
        <span className="workflow-page-guide-task">{localize(guideCopy.task)}</span>
        <span className="workflow-page-guide-meaning">{localize(guideCopy.meaning)}</span>
        <span className="workflow-page-guide-skip-hint" aria-hidden="true">
          {translate('点击任意位置跳过教学', 'Click anywhere to skip')}
        </span>
      </aside>,
      titleHost,
    )
    : null;
  const spotlightVisible = Boolean(
    presented
    && guidePageKey
    && spotlightRect?.key === guidePageKey
    && spotlightRect.visitKey === visitKey,
  );
  const overlay = spotlightVisible && spotlightRect
    ? createPortal(
      <>
        <div
          className={`workflow-page-guide-spotlight${isActive ? ' is-active' : ''}${isExiting ? ' is-exiting' : ''}`}
          aria-hidden="true"
          onTransitionEnd={handleSpotlightTransitionEnd}
          style={{
            top: spotlightRect.top + spotlightRect.height,
            left: spotlightRect.left,
            width: spotlightRect.width,
          }}
        />
        <button
          className={`workflow-page-guide-dismiss-surface${isExiting ? ' is-exiting' : ''}${isActive ? ' is-active' : ''}`}
          type="button"
          tabIndex={-1}
          aria-label={translate('关闭页面教学提示', 'Dismiss page guidance')}
          onClick={dismiss}
        />
      </>,
      document.body,
    )
    : null;

  return (
    <WorkflowPageGuideContext.Provider value={contextValue}>
      {children}
      {guide}
      {overlay}
    </WorkflowPageGuideContext.Provider>
  );
}

export function WorkflowPageGuideProvider({ children }: { children: ReactNode }) {
  if (!isWorkflowPageGuideEnabled()) {
    return (
      <WorkflowPageGuideContext.Provider value={DISABLED_WORKFLOW_PAGE_GUIDE}>
        {children}
      </WorkflowPageGuideContext.Provider>
    );
  }

  return <EnabledWorkflowPageGuideProvider>{children}</EnabledWorkflowPageGuideProvider>;
}

export function useOptionalWorkflowPageGuide() {
  return useContext(WorkflowPageGuideContext);
}

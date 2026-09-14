import {
  List,
  PanelLeftClose,
  PanelLeftOpen,
  X,
} from 'lucide-react';
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import type { MethodSectionConfig } from '../../content/method-article-config';
import { useLanguage } from '../../i18n/LanguageContext';

const COLLAPSED_STORAGE_KEY = 'leadership-feedback.method-navigation-collapsed';

interface MethodArticleShellProps {
  methodId: string;
  title: string;
  sections: readonly MethodSectionConfig[];
  hero: ReactNode;
  children: ReactNode;
}

function readCollapsedPreference() {
  if (typeof window === 'undefined') return false;
  try {
    return window.localStorage.getItem(COLLAPSED_STORAGE_KEY) === 'true';
  } catch {
    return false;
  }
}

export function MethodArticleShell({
  methodId,
  title,
  sections,
  hero,
  children,
}: MethodArticleShellProps) {
  const [collapsed, setCollapsed] = useState(readCollapsedPreference);

  const updateCollapsed = useCallback((nextValue: boolean) => {
    setCollapsed(nextValue);
    try {
      window.localStorage.setItem(COLLAPSED_STORAGE_KEY, String(nextValue));
    } catch {
      // Storage can be unavailable in hardened or private browsing modes.
    }
  }, []);

  return (
    <>
      {hero}
      <div className={`method-article-layout${collapsed ? ' is-navigation-collapsed' : ''}`}>
        <MethodArticleNavigation
          methodId={methodId}
          title={title}
          sections={sections}
          collapsed={collapsed}
          onCollapsedChange={updateCollapsed}
        />
        <div className="toolkit-method-body" data-method-article={methodId}>
          {children}
        </div>
      </div>
    </>
  );
}

interface MethodArticleNavigationProps {
  methodId: string;
  title: string;
  sections: readonly MethodSectionConfig[];
  collapsed: boolean;
  onCollapsedChange: (collapsed: boolean) => void;
}

function MethodArticleNavigation({
  methodId,
  title,
  sections,
  collapsed,
  onCollapsedChange,
}: MethodArticleNavigationProps) {
  const { translate } = useLanguage();
  const [activeSectionId, setActiveSectionId] = useState(sections[0]?.id ?? '');
  const [mobileOpen, setMobileOpen] = useState(false);
  const mobileTriggerRef = useRef<HTMLButtonElement>(null);
  const mobileCloseRef = useRef<HTMLButtonElement>(null);

  const replaceHash = useCallback((sectionId: string) => {
    const nextHash = `#${sectionId}`;
    if (window.location.hash === nextHash) return;
    window.history.replaceState(window.history.state, '', `${window.location.pathname}${window.location.search}${nextHash}`);
  }, []);

  const scrollToSection = useCallback((sectionId: string, pushHistory = true) => {
    const target = document.getElementById(sectionId);
    if (!target) return false;

    const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (pushHistory && window.location.hash !== `#${sectionId}`) {
      window.history.pushState(window.history.state, '', `${window.location.pathname}${window.location.search}#${sectionId}`);
    }
    setActiveSectionId(sectionId);
    target.scrollIntoView({ behavior: reduceMotion ? 'auto' : 'smooth', block: 'start' });
    return true;
  }, []);

  useEffect(() => {
    setActiveSectionId(sections[0]?.id ?? '');
    setMobileOpen(false);
  }, [methodId, sections]);

  useEffect(() => {
    let animationFrame = 0;
    const contentRoot = document.querySelector(`[data-method-article="${CSS.escape(methodId)}"]`);

    const updateActiveSection = () => {
      animationFrame = 0;
      const activationLine = Math.min(220, window.innerHeight * 0.28);
      let nextSectionId = sections[0]?.id ?? '';
      let hasReachedArticle = false;

      sections.forEach((section) => {
        const element = document.getElementById(section.id);
        if (!element) return;
        if (element.getBoundingClientRect().top <= activationLine) {
          nextSectionId = section.id;
          hasReachedArticle = true;
        }
      });

      if (nextSectionId) {
        setActiveSectionId((current) => current === nextSectionId ? current : nextSectionId);
      }
      if (hasReachedArticle && nextSectionId) replaceHash(nextSectionId);
    };

    const scheduleUpdate = () => {
      if (animationFrame) return;
      animationFrame = window.requestAnimationFrame(updateActiveSection);
    };

    const mutationObserver = contentRoot
      ? new MutationObserver(scheduleUpdate)
      : null;
    mutationObserver?.observe(contentRoot as Node, { childList: true, subtree: true });

    scheduleUpdate();
    window.addEventListener('scroll', scheduleUpdate, { passive: true });
    window.addEventListener('resize', scheduleUpdate);
    return () => {
      if (animationFrame) window.cancelAnimationFrame(animationFrame);
      mutationObserver?.disconnect();
      window.removeEventListener('scroll', scheduleUpdate);
      window.removeEventListener('resize', scheduleUpdate);
    };
  }, [methodId, replaceHash, sections]);

  useEffect(() => {
    let retryTimer = 0;
    let retries = 0;

    const restoreHashPosition = () => {
      const sectionId = decodeURIComponent(window.location.hash.slice(1));
      if (!sections.some((section) => section.id === sectionId)) return;
      if (scrollToSection(sectionId, false)) return;
      if (retries >= 20) return;
      retries += 1;
      retryTimer = window.setTimeout(restoreHashPosition, 50);
    };

    const handleHistoryNavigation = () => {
      retries = 0;
      restoreHashPosition();
    };

    restoreHashPosition();
    window.addEventListener('hashchange', handleHistoryNavigation);
    window.addEventListener('popstate', handleHistoryNavigation);
    return () => {
      window.clearTimeout(retryTimer);
      window.removeEventListener('hashchange', handleHistoryNavigation);
      window.removeEventListener('popstate', handleHistoryNavigation);
    };
  }, [methodId, scrollToSection, sections]);

  useEffect(() => {
    if (!mobileOpen) return undefined;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    window.requestAnimationFrame(() => mobileCloseRef.current?.focus());

    const containDialogFocus = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setMobileOpen(false);
        window.requestAnimationFrame(() => mobileTriggerRef.current?.focus());
        return;
      }
      if (event.key !== 'Tab') return;

      const dialog = mobileCloseRef.current?.closest<HTMLElement>('[role="dialog"]');
      const focusable = dialog
        ? Array.from(dialog.querySelectorAll<HTMLElement>('button:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])'))
        : [];
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    window.addEventListener('keydown', containDialogFocus);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener('keydown', containDialogFocus);
    };
  }, [mobileOpen]);

  const selectSection = (sectionId: string) => {
    scrollToSection(sectionId);
    setMobileOpen(false);
  };

  const sectionLinks = (mobile = false) => (
    <ol className="method-section-list">
      {sections.map((section, index) => {
        const active = section.id === activeSectionId;
        return (
          <li key={section.id}>
            <button
              type="button"
              className={active ? 'is-active' : undefined}
              aria-current={active ? 'location' : undefined}
              aria-label={`${String(index + 1).padStart(2, '0')} ${section.label}`}
              title={!mobile && collapsed ? section.label : undefined}
              onClick={() => selectSection(section.id)}
            >
              <span className="method-section-index">{String(index + 1).padStart(2, '0')}</span>
              <span className="method-section-marker" aria-hidden="true" />
              <span className="method-section-label">{section.label}</span>
            </button>
          </li>
        );
      })}
    </ol>
  );

  return (
    <>
      <aside
        className={`method-navigation${collapsed ? ' is-collapsed' : ''}`}
        aria-label={`${title} ${translate('页内导航', 'in-page navigation')}`}
      >
        <header>
          <div>
            <small>METHOD GUIDE</small>
            <strong>{translate('本页内容', 'On this page')}</strong>
          </div>
          <button
            type="button"
            className="method-navigation-toggle"
            aria-label={collapsed
              ? translate('展开页内导航', 'Expand in-page navigation')
              : translate('收起页内导航', 'Collapse in-page navigation')}
            title={collapsed
              ? translate('展开页内导航', 'Expand in-page navigation')
              : translate('收起页内导航', 'Collapse in-page navigation')}
            onClick={() => onCollapsedChange(!collapsed)}
          >
            {collapsed ? <PanelLeftOpen aria-hidden="true" /> : <PanelLeftClose aria-hidden="true" />}
          </button>
        </header>
        <nav aria-label={translate('方法章节', 'Method sections')}>
          {sectionLinks()}
        </nav>
      </aside>

      <button
        ref={mobileTriggerRef}
        type="button"
        className="method-navigation-mobile-trigger"
        aria-label={translate('打开页内导航', 'Open in-page navigation')}
        aria-expanded={mobileOpen}
        onClick={() => setMobileOpen(true)}
      >
        <List aria-hidden="true" />
      </button>

      {mobileOpen && (
        <div className="method-navigation-mobile-layer">
          <button
            type="button"
            className="method-navigation-mobile-backdrop"
            aria-label={translate('关闭页内导航', 'Close in-page navigation')}
            onClick={() => setMobileOpen(false)}
          />
          <aside
            className="method-navigation-mobile"
            role="dialog"
            aria-modal="true"
            aria-label={`${title} ${translate('页内导航', 'in-page navigation')}`}
          >
            <header>
              <div><small>METHOD GUIDE</small><strong>{translate('本页内容', 'On this page')}</strong></div>
              <button
                ref={mobileCloseRef}
                type="button"
                aria-label={translate('关闭页内导航', 'Close in-page navigation')}
                title={translate('关闭页内导航', 'Close in-page navigation')}
                onClick={() => setMobileOpen(false)}
              >
                <X aria-hidden="true" />
              </button>
            </header>
            <nav aria-label={translate('方法章节', 'Method sections')}>
              {sectionLinks(true)}
            </nav>
          </aside>
        </div>
      )}
    </>
  );
}

import {
  ArrowRight,
  ArrowUpRight,
  Menu,
  X,
} from 'lucide-react';
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type CSSProperties,
  type ReactNode,
} from 'react';
import { Link } from 'react-router-dom';
import { STATIC_ASSETS } from '../config/staticAssets';
import type {
  HomeExperienceContent,
  LandingNavGroup,
} from '../content/home-experience-content';
import { useLanguage } from '../i18n/LanguageContext';
import { useBackgroundParallax } from '../hooks/useBackgroundParallax';
import { AccountActions } from './AccountActions';
import { LanguageSelector } from './LanguageSelector';
import { LandingSiteSearch } from './LandingSiteSearch';

interface SmartLinkProps {
  href: string;
  className?: string;
  children: ReactNode;
  onClick?: () => void;
  ariaLabel?: string;
}

interface LandingHeaderProps {
  navigation: readonly LandingNavGroup[];
  homeHref?: string;
  homeAriaLabel?: string;
  anchorBasePath?: string;
  utilityAction?: ReactNode;
  utilityPanel?: ReactNode;
  inverse?: boolean;
  transparent?: boolean;
  light?: boolean;
}

interface LandingFooterProps {
  anchorBasePath?: string;
}

const SOLUTION_AUTOPLAY_MS = 5200;

function SmartLink({ href, className, children, onClick, ariaLabel }: SmartLinkProps) {
  if (href.startsWith('/')) {
    return <Link className={className} to={href} onClick={onClick} aria-label={ariaLabel}>{children}</Link>;
  }

  if (href.startsWith('#')) {
    return <a className={className} href={href} onClick={onClick} aria-label={ariaLabel}>{children}</a>;
  }

  return (
    <a
      className={className}
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      onClick={onClick}
      aria-label={ariaLabel}
    >
      {children}
    </a>
  );
}

export function LandingHeader({
  navigation,
  homeHref = '#home-top',
  homeAriaLabel,
  anchorBasePath = '',
  utilityAction,
  utilityPanel,
  inverse = false,
  transparent = false,
  light = false,
}: LandingHeaderProps) {
  const { translate } = useLanguage();
  const [openNavId, setOpenNavId] = useState<string | null>(null);
  const [displayedNavId, setDisplayedNavId] = useState<string | null>(null);
  const [mobileOpen, setMobileOpen] = useState(false);
  const [searchOpen, setSearchOpen] = useState(false);
  const navIntentRef = useRef<number | null>(null);
  const activeGroup = navigation.find(({ id }) => id === displayedNavId);
  const resolveHref = (href: string) => (
    href.startsWith('#') && anchorBasePath ? `${anchorBasePath}${href}` : href
  );

  const clearNavIntent = useCallback(() => {
    if (navIntentRef.current === null) return;
    window.clearTimeout(navIntentRef.current);
    navIntentRef.current = null;
  }, []);

  const showNavigation = useCallback((navId: string) => {
    clearNavIntent();
    setDisplayedNavId(navId);
    setOpenNavId(navId);
  }, [clearNavIntent]);

  const scheduleNavigation = useCallback((navId: string) => {
    clearNavIntent();
    navIntentRef.current = window.setTimeout(() => {
      navIntentRef.current = null;
      setDisplayedNavId(navId);
      setOpenNavId(navId);
    }, 90);
  }, [clearNavIntent]);

  const hideDesktopNavigation = useCallback(() => {
    clearNavIntent();
    setOpenNavId(null);
  }, [clearNavIntent]);

  const closeNavigation = useCallback(() => {
    hideDesktopNavigation();
    setMobileOpen(false);
  }, [hideDesktopNavigation]);

  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      closeNavigation();
    };
    window.addEventListener('keydown', closeOnEscape);
    return () => window.removeEventListener('keydown', closeOnEscape);
  }, [closeNavigation]);

  useEffect(() => () => clearNavIntent(), [clearNavIntent]);

  return (
    <header
      className={`home-header${inverse ? ' is-inverse' : ''}${transparent ? ' is-transparent' : ''}${light ? ' is-light' : ''}${searchOpen ? ' is-searching' : ''}`}
      onMouseLeave={hideDesktopNavigation}
    >
      <div
        className="home-header-inner"
        aria-hidden={searchOpen || undefined}
        inert={searchOpen || undefined}
      >
        <SmartLink
          className="home-brand-row"
          href={homeHref}
          ariaLabel={homeAriaLabel
            ? translate(homeAriaLabel)
            : translate(homeHref === '#home-top' ? '返回首页顶部' : '返回首页')}
        >
          <img
            className="home-company-logo"
            src={light || (transparent && !inverse) ? STATIC_ASSETS.companyLogoBlack : STATIC_ASSETS.companyLogoWhite}
            alt="Bosch"
          />
        </SmartLink>

        <nav className="home-desktop-nav" aria-label={translate('首页主导航')}>
          {navigation.map((group) => {
            const isOpen = openNavId === group.id;
            return (
              <Link
                key={group.id}
                to={resolveHref(group.href)}
                aria-expanded={isOpen}
                aria-controls="home-mega-panel"
                aria-haspopup="true"
                onMouseEnter={() => scheduleNavigation(group.id)}
                onFocus={() => showNavigation(group.id)}
                onClick={closeNavigation}
              >
                <span>{translate(group.label)}</span>
              </Link>
            );
          })}
        </nav>

        <div className="home-header-actions">
          {utilityAction}
          <LanguageSelector
            className="home-language-button"
            compact
            variant="popover"
            onOpen={closeNavigation}
          />
          <LandingSiteSearch onOpen={closeNavigation} onOpenChange={setSearchOpen} />
          <AccountActions variant="display-name" />
          <button
            className="home-mobile-menu-button"
            type="button"
            aria-expanded={mobileOpen}
            aria-controls="home-mobile-navigation"
            aria-label={translate(mobileOpen ? '关闭导航' : '打开导航')}
            onClick={() => setMobileOpen((open) => !open)}
          >
            {mobileOpen ? <X aria-hidden="true" /> : <Menu aria-hidden="true" />}
          </button>
        </div>
      </div>

      {utilityPanel}

      <div
        id="home-mega-panel"
        className={`home-mega-panel${openNavId ? ' is-open' : ''}`}
        aria-hidden={!openNavId}
        inert={!openNavId}
      >
        {activeGroup && (
          <div className="home-mega-panel-inner" key={activeGroup.id}>
            <div className="home-mega-summary">
              <span>{translate(activeGroup.label)}</span>
              <p>{translate(activeGroup.description, activeGroup.descriptionEnglish)}</p>
              <SmartLink href={resolveHref(activeGroup.href)} onClick={closeNavigation}>
                {translate('探索全部')} <ArrowRight aria-hidden="true" />
              </SmartLink>
            </div>
            <div className="home-mega-links">
              {activeGroup.links.map((link, index) => (
                <SmartLink href={resolveHref(link.href)} key={`${link.href}-${index}`} onClick={closeNavigation}>
                  <small>{String(index + 1).padStart(2, '0')}</small>
                  <span>{translate(link.label, link.labelEnglish)}</span>
                  <ArrowUpRight aria-hidden="true" />
                </SmartLink>
              ))}
            </div>
          </div>
        )}
      </div>

      <nav
        id="home-mobile-navigation"
        className={`home-mobile-navigation${mobileOpen ? ' is-open' : ''}`}
        aria-hidden={!mobileOpen}
      >
        <div>
          {navigation.map((group) => (
            <section className="home-mobile-nav-group" key={group.id}>
              <SmartLink href={resolveHref(group.href)} onClick={closeNavigation}>{translate(group.label)}</SmartLink>
              <p>{translate(group.description, group.descriptionEnglish)}</p>
              <div>
                {group.links.map((link, index) => (
                  <SmartLink href={resolveHref(link.href)} key={`${link.href}-${index}`} onClick={closeNavigation}>
                    {translate(link.label, link.labelEnglish)}
                  </SmartLink>
                ))}
              </div>
            </section>
          ))}
        </div>
      </nav>
    </header>
  );
}

export function LandingHero({ hero }: { hero: HomeExperienceContent['hero'] }) {
  const { translate } = useLanguage();

  return (
    <section id="home-top" className="home-hero-shell" aria-labelledby="home-hero-title">
      <div className="home-hero">
        <picture className="home-hero-media">
          <source media="(max-width: 680px)" srcSet={hero.mobileImage} />
          <img src={hero.image} alt={translate(hero.imageAlt)} fetchPriority="high" decoding="async" />
        </picture>
        <div className="home-hero-overlay" aria-hidden="true" />
        <div className="home-hero-inner">
          <article className="home-hero-copy">
            <h1 id="home-hero-title" aria-label={translate(hero.title)}>
              {hero.titleLines.map((line) => (
                <span key={line}>{translate(line)}</span>
              ))}
            </h1>
          </article>
        </div>
      </div>
    </section>
  );
}

export function LandingResourceCenter({
  resources,
}: {
  resources: HomeExperienceContent['resourceModules'];
}) {
  const { translate, translateTemplate } = useLanguage();
  const solutions = resources;
  const [activeIndex, setActiveIndex] = useState(0);
  const [isPointerInteracting, setIsPointerInteracting] = useState(false);
  const [isFocusInteracting, setIsFocusInteracting] = useState(false);
  const [isInViewport, setIsInViewport] = useState(false);
  const [isPageVisible, setIsPageVisible] = useState(() => document.visibilityState !== 'hidden');
  const [prefersReducedMotion, setPrefersReducedMotion] = useState(
    () => window.matchMedia('(prefers-reduced-motion: reduce)').matches,
  );
  const sectionRef = useRef<HTMLElement>(null);
  const tabsRef = useRef<HTMLElement>(null);
  const tabRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const hoverIntentRef = useRef<number | null>(null);
  const solutionCount = solutions.length;
  const activeSolution = solutions[activeIndex];

  useEffect(() => {
    const mediaQuery = window.matchMedia('(prefers-reduced-motion: reduce)');
    const updateMotionPreference = () => setPrefersReducedMotion(mediaQuery.matches);
    const updateVisibility = () => setIsPageVisible(document.visibilityState !== 'hidden');
    mediaQuery.addEventListener('change', updateMotionPreference);
    document.addEventListener('visibilitychange', updateVisibility);
    return () => {
      mediaQuery.removeEventListener('change', updateMotionPreference);
      document.removeEventListener('visibilitychange', updateVisibility);
    };
  }, []);

  useEffect(() => {
    const node = sectionRef.current;
    if (!node) return undefined;
    if (typeof IntersectionObserver === 'undefined') {
      setIsInViewport(true);
      return undefined;
    }
    const observer = new IntersectionObserver(([entry]) => {
      setIsInViewport(Boolean(entry?.isIntersecting));
    }, { threshold: 0.08 });
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    const tabs = tabsRef.current;
    const activeTab = tabRefs.current[activeIndex];
    if (!tabs || !activeTab || tabs.scrollWidth <= tabs.clientWidth) return;
    const centeredLeft = activeTab.offsetLeft - (tabs.clientWidth - activeTab.offsetWidth) / 2;
    const maxLeft = tabs.scrollWidth - tabs.clientWidth;
    tabs.scrollTo({
      left: Math.max(0, Math.min(centeredLeft, maxLeft)),
      behavior: prefersReducedMotion ? 'auto' : 'smooth',
    });
  }, [activeIndex, prefersReducedMotion]);

  useEffect(() => {
    if (!isInViewport || solutionCount < 2) return undefined;
    const nextImage = new Image();
    nextImage.decoding = 'async';
    nextImage.src = solutions[(activeIndex + 1) % solutionCount].image;
    return undefined;
  }, [activeIndex, isInViewport, solutionCount, solutions]);

  const clearHoverIntent = useCallback(() => {
    if (hoverIntentRef.current === null) return;
    window.clearTimeout(hoverIntentRef.current);
    hoverIntentRef.current = null;
  }, []);

  useEffect(() => () => clearHoverIntent(), [clearHoverIntent]);

  const selectSolution = useCallback((index: number) => {
    clearHoverIntent();
    setActiveIndex(index);
  }, [clearHoverIntent]);

  const previewSolution = useCallback((index: number) => {
    clearHoverIntent();
    if (index === activeIndex) return;
    hoverIntentRef.current = window.setTimeout(() => {
      hoverIntentRef.current = null;
      setActiveIndex(index);
    }, 90);
  }, [activeIndex, clearHoverIntent]);

  const autoplayEnabled = solutionCount > 1
    && isInViewport
    && isPageVisible
    && !prefersReducedMotion;
  const isInteracting = isPointerInteracting || isFocusInteracting;
  const autoplayRunning = autoplayEnabled && !isInteracting;
  const autoplayStyle = {
    '--home-solution-autoplay-duration': `${SOLUTION_AUTOPLAY_MS}ms`,
  } as CSSProperties;

  return (
    <section
      id="home-resources"
      ref={sectionRef}
      className={`home-resource-modules${autoplayEnabled ? ' is-autoplay-enabled' : ''}${autoplayEnabled && !autoplayRunning ? ' is-autoplay-paused' : ''}`}
      style={autoplayStyle}
      aria-labelledby="home-resources-title"
      aria-roledescription={translate('轮播')}
      onFocusCapture={() => setIsFocusInteracting(true)}
      onBlurCapture={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setIsFocusInteracting(false);
      }}
    >
      <div className="home-section-inner">
        <header className="home-solution-header" data-home-reveal="headline">
          <div className="home-section-heading">
            <p>{translate('资源中心', 'Resource Center')}</p>
            <h2 id="home-resources-title">{translate('绩效管理的五个维度', 'Five dimensions of performance management')}</h2>
          </div>
          <SmartLink className="home-solution-all-link" href="/resources">
            {translate('查看全部资源', 'Explore all resources')} <ArrowRight aria-hidden="true" />
          </SmartLink>
        </header>

        <div
          className="home-solution-stage"
          data-home-reveal="media"
          onMouseEnter={() => setIsPointerInteracting(true)}
          onMouseLeave={() => setIsPointerInteracting(false)}
        >
          <article
            id="home-solution-panel"
            className="home-solution-panel"
            role="tabpanel"
            aria-labelledby={`home-solution-tab-${activeSolution.id}`}
            key={activeSolution.id}
          >
            <div className="home-solution-copy">
              <p
                className="home-solution-kicker"
                aria-label={translateTemplate('第 {index} 项', 'Item {index}', { index: activeIndex + 1 })}
              >
                {String(activeIndex + 1).padStart(2, '0')}
              </p>
              <h3>{translate(activeSolution.title, activeSolution.titleEnglish)}</h3>
              <p className="home-solution-description">
                {translate(activeSolution.description, activeSolution.descriptionEnglish)}
              </p>
              <SmartLink className="home-solution-panel-link" href={activeSolution.href}>
                {translate('阅读本模块', 'Read this module')} <ArrowRight aria-hidden="true" />
              </SmartLink>
            </div>
            <figure>
              <img
                src={activeSolution.image}
                alt={translate(activeSolution.imageAlt, activeSolution.imageAltEnglish)}
                loading="lazy"
                fetchPriority="low"
                decoding="async"
              />
            </figure>
          </article>
        </div>

        <nav
          ref={tabsRef}
          className="home-solution-tabs"
          aria-label={translate('资源中心模块轮播', 'Resource center module carousel')}
          role="tablist"
          data-home-reveal="rise-late"
        >
          {solutions.map((solution, index) => {
            const isActive = index === activeIndex;
            return (
              <button
                id={`home-solution-tab-${solution.id}`}
                className={isActive ? 'is-active' : undefined}
                type="button"
                role="tab"
                aria-controls="home-solution-panel"
                aria-selected={isActive}
                tabIndex={isActive ? 0 : -1}
                key={solution.id}
                ref={(node) => {
                  tabRefs.current[index] = node;
                }}
                onClick={() => selectSolution(index)}
                onMouseEnter={() => {
                  setIsPointerInteracting(true);
                  previewSolution(index);
                }}
                onMouseLeave={() => {
                  clearHoverIntent();
                  setIsPointerInteracting(false);
                }}
                onFocus={() => selectSolution(index)}
                onKeyDown={(event) => {
                  if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
                  event.preventDefault();
                  const nextIndex = event.key === 'Home'
                    ? 0
                    : event.key === 'End'
                      ? solutionCount - 1
                      : (index + (event.key === 'ArrowRight' ? 1 : -1) + solutionCount) % solutionCount;
                  selectSolution(nextIndex);
                  requestAnimationFrame(() => tabRefs.current[nextIndex]?.focus());
                }}
              >
                <span className="home-solution-progress" aria-hidden="true">
                  <span
                    onAnimationEnd={() => {
                      if (isActive && autoplayRunning) {
                        setActiveIndex((current) => (current + 1) % solutionCount);
                      }
                    }}
                  />
                </span>
                <span className="home-solution-label">
                  {translate(solution.label, solution.labelEnglish)}
                </span>
              </button>
            );
          })}
        </nav>
      </div>
    </section>
  );
}

export function LandingSolutions({ solutions }: { solutions: HomeExperienceContent['solutions'] }) {
  const { translate, translateTemplate } = useLanguage();

  return (
    <section
      id="home-solutions"
      className="home-solutions-approach"
      aria-labelledby="home-solutions-title"
    >
      <div className="home-section-inner">
        <header className="home-approach-header" data-home-reveal="headline">
          <p>{translate('解决方案', 'Solutions')}</p>
          <h2 id="home-solutions-title">
            {translate('从清晰判断到持续发展，让方法真正服务于行动', 'From clear judgment to lasting development')}
          </h2>
          <p className="home-approach-intro">
            {translate(
              '将战略分析、教练目标、反馈评估与发展框架连接起来，为不同人才议题提供可直接应用的方法。',
              'Connect strategy, coaching, feedback, assessment, and development into practical methods for real talent decisions.',
            )}
          </p>
        </header>

        <div className="home-approach-list" data-home-reveal="rise-late">
          {solutions.map((solution) => (
            <article className="home-approach-card" key={solution.id}>
              <SmartLink
                className="home-approach-card-link"
                href={solution.href}
                ariaLabel={translateTemplate('查看 {title}', 'Explore {title}', { title: solution.title })}
              >
                <figure className="home-approach-card-media">
                  <img
                    src={solution.image}
                    alt={translate(solution.imageAlt)}
                    loading="lazy"
                    fetchPriority="low"
                    decoding="async"
                  />
                </figure>
                <div className="home-approach-card-copy">
                  <h3>{solution.title}</h3>
                  <p>{translate(solution.description, solution.descriptionEnglish)}</p>
                  <span className="home-approach-card-action" aria-hidden="true">
                    <ArrowRight aria-hidden="true" />
                  </span>
                </div>
              </SmartLink>
            </article>
          ))}
        </div>

        <footer className="home-approach-footer" data-home-reveal="rise-late">
          <SmartLink className="home-approach-all-link" href="/solutions">
            {translate('查看全部解决方案', 'Explore all solutions')}
            <ArrowRight aria-hidden="true" />
          </SmartLink>
        </footer>
      </div>
    </section>
  );
}

export function LandingAbout({ content }: { content: HomeExperienceContent['about'] }) {
  const { translate } = useLanguage();
  const sectionRef = useRef<HTMLElement>(null);
  const mediaRef = useRef<HTMLPictureElement>(null);
  useBackgroundParallax(sectionRef, mediaRef);

  return (
    <section ref={sectionRef} id="home-about" className="home-about" aria-labelledby="home-about-title">
      <picture ref={mediaRef} className="home-about-media" aria-hidden="true">
        <source media="(max-width: 720px)" srcSet={content.mobileImage} />
        <img src={content.image} alt="" width="1800" height="720" loading="lazy" fetchPriority="low" decoding="async" />
      </picture>
      <div className="home-about-copy" data-home-reveal="rise-delayed">
        <p>{translate(content.eyebrow)}</p>
        <h2 id="home-about-title">{translate(content.title)}</h2>
        <span>{translate(content.description)}</span>
        <SmartLink href="#home-footer" className="home-about-cta">{translate('联系我们')} <ArrowRight aria-hidden="true" /></SmartLink>
      </div>
    </section>
  );
}

export function LandingFooter({ anchorBasePath = '' }: LandingFooterProps = {}) {
  const { translate } = useLanguage();
  const resolveHref = (href: string) => (
    href.startsWith('#') && anchorBasePath ? `${anchorBasePath}${href}` : href
  );

  return (
    <footer id="home-footer" className="home-footer">
      <div className="home-footer-main">
        <div className="home-section-inner">
          <div className="home-footer-brand" data-home-reveal="rise">
            <img src={STATIC_ASSETS.companyLogoBlack} alt="Bosch" loading="lazy" decoding="async" />
            <strong>Performance Feedback</strong>
            <p>{translate('连接人才洞察、专业资源与真实管理实践。')}</p>
          </div>
          <nav aria-label={translate('首页页脚导航')} data-home-reveal="rise-delayed">
            <div>
              <strong>{translate('关于我们')}</strong>
              <SmartLink href={resolveHref('#home-about')}>{translate('平台介绍')}</SmartLink>
              <a href="#home-footer">{translate('联系我们')}</a>
            </div>
            <div>
              <strong>{translate('解决方案')}</strong>
              <SmartLink href={resolveHref('#home-solutions')}>{translate('人才咨询')}</SmartLink>
              <SmartLink href={resolveHref('#home-solutions')}>{translate('测评诊断')}</SmartLink>
              <SmartLink href={resolveHref('#home-solutions')}>{translate('发展培养')}</SmartLink>
              <SmartLink href={resolveHref('#home-solutions')}>{translate('HR 赋能')}</SmartLink>
            </div>
            <div>
              <strong>{translate('资源中心')}</strong>
              <Link to="/resources">{translate('绩效管理')}</Link>
            </div>
          </nav>
          <div className="home-footer-meta" data-home-reveal="rise-late">
            <span>{translate('仅限授权用户使用')}</span>
            <span>© {new Date().getFullYear()} Bosch</span>
          </div>
        </div>
      </div>
    </footer>
  );
}

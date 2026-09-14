import {
  ArrowLeft,
  ArrowRight,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  List,
} from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { LandingFooter, LandingHeader } from '../components/LandingExperience';
import { RouteRedirect } from '../components/RouteLoading';
import {
  getPerformanceManagementDimension,
  isPerformanceManagementDimensionSlug,
  performanceManagementDimensions,
} from '../content/performance-management-content';
import { performanceManagementDocumentChapters } from '../content/performance-management-document';
import { homeExperienceContent } from '../content/home-experience-content';
import { useLanguage } from '../i18n/LanguageContext';
import '../styles/home-page.css';
import '../styles/performance-management-detail.css';

export default function PerformanceManagementDetailPage() {
  const { dimensionSlug } = useParams<{ dimensionSlug: string }>();
  const { translate, translateTemplate } = useLanguage();
  const [railCollapsed, setRailCollapsed] = useState(false);
  const [activeSectionId, setActiveSectionId] = useState<string>();
  const railRef = useRef<HTMLElement>(null);
  const dimension = dimensionSlug && isPerformanceManagementDimensionSlug(dimensionSlug)
    ? getPerformanceManagementDimension(dimensionSlug)
    : undefined;
  const chapter = dimension ? performanceManagementDocumentChapters[dimension.slug] : undefined;

  useEffect(() => {
    document.title = dimension
      ? translateTemplate(
        '{title} | Performance Feedback',
        '{title} | Performance Feedback',
        { title: translate(dimension.title, dimension.englishTitle) },
      )
      : translate('绩效管理资源中心', 'Performance Management Resource Center');
  }, [dimension, translate, translateTemplate]);

  useEffect(() => {
    const sectionIds = chapter?.sections.map((section) => section.id) ?? [];
    if (sectionIds.length === 0) {
      setActiveSectionId(undefined);
      return undefined;
    }

    let animationFrame = 0;
    const updateActiveSection = () => {
      animationFrame = 0;
      const readingLine = Math.min(220, Math.max(96, window.innerHeight * 0.22));
      let nextSectionId = sectionIds[0];

      for (const sectionId of sectionIds) {
        const sectionElement = document.getElementById(sectionId);
        if (!sectionElement || sectionElement.getBoundingClientRect().top > readingLine) break;
        nextSectionId = sectionId;
      }

      const pageBottom = window.scrollY + window.innerHeight;
      if (pageBottom >= document.documentElement.scrollHeight - 4) {
        nextSectionId = sectionIds[sectionIds.length - 1];
      }

      setActiveSectionId((currentSectionId) => (
        currentSectionId === nextSectionId ? currentSectionId : nextSectionId
      ));
    };
    const scheduleActiveSectionUpdate = () => {
      if (animationFrame !== 0) return;
      animationFrame = window.requestAnimationFrame(updateActiveSection);
    };

    setActiveSectionId(sectionIds[0]);
    scheduleActiveSectionUpdate();
    window.addEventListener('scroll', scheduleActiveSectionUpdate, { passive: true });
    window.addEventListener('resize', scheduleActiveSectionUpdate);

    return () => {
      window.removeEventListener('scroll', scheduleActiveSectionUpdate);
      window.removeEventListener('resize', scheduleActiveSectionUpdate);
      if (animationFrame !== 0) window.cancelAnimationFrame(animationFrame);
    };
  }, [chapter]);

  useEffect(() => {
    if (!activeSectionId || railCollapsed) return undefined;

    const animationFrame = window.requestAnimationFrame(() => {
      const rail = railRef.current;
      if (!rail) return;

      const activeLink = Array.from(
        rail.querySelectorAll<HTMLAnchorElement>('[data-section-id]'),
      ).find((link) => link.dataset.sectionId === activeSectionId);
      if (!activeLink) return;

      const railRect = rail.getBoundingClientRect();
      const linkRect = activeLink.getBoundingClientRect();
      const toolbar = rail.querySelector<HTMLElement>('.pm-article-rail-toolbar');
      const visibleTop = railRect.top + (toolbar?.offsetHeight ?? 0) + 8;
      const visibleBottom = railRect.bottom - 12;
      let scrollDelta = 0;

      if (linkRect.top < visibleTop) {
        scrollDelta = linkRect.top - visibleTop;
      } else if (linkRect.bottom > visibleBottom) {
        scrollDelta = linkRect.bottom - visibleBottom;
      }

      if (Math.abs(scrollDelta) > 1) {
        rail.scrollTo({
          top: Math.max(0, rail.scrollTop + scrollDelta),
          behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth',
        });
      }
    });

    return () => window.cancelAnimationFrame(animationFrame);
  }, [activeSectionId, railCollapsed]);

  if (!dimension || !chapter) return <RouteRedirect to="/resources" />;

  const currentIndex = performanceManagementDimensions.findIndex((item) => item.slug === dimension.slug);
  const previousDimension = currentIndex > 0 ? performanceManagementDimensions[currentIndex - 1] : undefined;
  const nextDimension = currentIndex < performanceManagementDimensions.length - 1
    ? performanceManagementDimensions[currentIndex + 1]
    : undefined;
  const previousChapter = previousDimension
    ? performanceManagementDocumentChapters[previousDimension.slug]
    : undefined;
  const nextChapter = nextDimension
    ? performanceManagementDocumentChapters[nextDimension.slug]
    : undefined;

  return (
    <div className="pm-detail-page">
      <a className="pm-skip-link" href="#pm-detail-content">
        {translate('跳至正文', 'Skip to content')}
      </a>
      <LandingHeader
        navigation={homeExperienceContent.navigation}
        homeHref="/"
        anchorBasePath="/"
        inverse
      />

      <main className="pm-detail-main">

      <header className="pm-article-masthead" aria-labelledby="pm-article-title">
        <div className="pm-article-shell pm-article-masthead-inner">
          <div className="pm-article-title-block">
            <p>{translate('绩效管理', 'Performance Management')}</p>
            <h1 id="pm-article-title">{chapter.title}</h1>
          </div>
          <div className="pm-article-meta" aria-label={translate('文章信息', 'Article information')}>
            <span>{translate('管理者指南', 'People Manager Guidebook')}</span>
            <span>{dimension.number} / {String(performanceManagementDimensions.length).padStart(2, '0')}</span>
          </div>
        </div>
      </header>

      {chapter.sections.length > 0 && (
        <details className="pm-article-mobile-toc">
          <summary>
            <span><List aria-hidden="true" /> {translate('本页内容', 'In this article')}</span>
            <ChevronDown aria-hidden="true" />
          </summary>
          <nav aria-label={translate('页内目录', 'Table of contents')}>
            <ol>
              {chapter.sections.map((section) => (
                <li key={section.id}>
                  <a
                    className={section.id === activeSectionId ? 'is-active' : undefined}
                    href={`#${section.id}`}
                    aria-current={section.id === activeSectionId ? 'location' : undefined}
                  >
                    {section.title}
                  </a>
                </li>
              ))}
            </ol>
          </nav>
        </details>
      )}

      <div className={`pm-article-shell pm-article-layout${chapter.sections.length === 0 ? ' has-no-rail' : ''}`}>
        {chapter.sections.length > 0 && (
          <aside
            ref={railRef}
            className={`pm-article-rail${railCollapsed ? ' is-collapsed' : ''}`}
            aria-label={translate('文章导航', 'Article navigation')}
          >
            <div className="pm-article-rail-toolbar">
              <button
                className="pm-article-rail-toggle"
                type="button"
                aria-controls="pm-article-rail-navigation"
                aria-expanded={!railCollapsed}
                aria-label={translate(
                  railCollapsed ? '展开页内导航' : '收起页内导航',
                  railCollapsed ? 'Expand article navigation' : 'Collapse article navigation',
                )}
                title={translate(
                  railCollapsed ? '展开页内导航' : '收起页内导航',
                  railCollapsed ? 'Expand article navigation' : 'Collapse article navigation',
                )}
                onClick={() => setRailCollapsed((collapsed) => !collapsed)}
              >
                {railCollapsed
                  ? <ChevronRight aria-hidden="true" />
                  : <ChevronLeft aria-hidden="true" />}
              </button>
            </div>
            <div
              id="pm-article-rail-navigation"
              className="pm-article-rail-content"
              aria-hidden={railCollapsed || undefined}
              inert={railCollapsed || undefined}
            >
              <nav aria-label={translate('页内目录', 'Table of contents')}>
                <p><List aria-hidden="true" /> {translate('本页内容', 'In this article')}</p>
                <ol>
                  {chapter.sections.map((section) => (
                    <li key={section.id}>
                      <a
                        className={section.id === activeSectionId ? 'is-active' : undefined}
                        href={`#${section.id}`}
                        data-section-id={section.id}
                        aria-current={section.id === activeSectionId ? 'location' : undefined}
                      >
                        {section.title}
                      </a>
                    </li>
                  ))}
                </ol>
              </nav>
            </div>
          </aside>
        )}

        <article
          className="pm-document-article"
          id="pm-detail-content"
          aria-labelledby="pm-article-title"
          data-source-document="绩效管理草稿.docx"
        >
          <div
            className="pm-document-body"
            dangerouslySetInnerHTML={{ __html: chapter.bodyHtml }}
          />
        </article>
      </div>

      <section className="pm-article-pagination" aria-label={translate('维度阅读导航', 'Dimension navigation')}>
        <picture className="pm-article-pagination-background" aria-hidden="true">
          <source media="(max-width: 720px)" srcSet={homeExperienceContent.about.mobileImage} />
          <img
            src={homeExperienceContent.about.image}
            alt=""
            loading="lazy"
            decoding="async"
          />
        </picture>
        <div className="pm-article-shell">
          <nav aria-label={translate('维度翻页', 'Dimension pagination')}>
            {previousDimension && previousChapter ? (
              <Link className="is-previous" to={previousDimension.path}>
                <ArrowLeft aria-hidden="true" />
                <span>
                  <small>{translate('上一维', 'Previous')}</small>
                  <strong>{previousChapter.title}</strong>
                </span>
              </Link>
            ) : null}
            {nextDimension && nextChapter ? (
              <Link className="is-next" to={nextDimension.path}>
                <span>
                  <small>{translate('下一维', 'Next')}</small>
                  <strong>{nextChapter.title}</strong>
                </span>
                <ArrowRight aria-hidden="true" />
              </Link>
            ) : null}
          </nav>
        </div>
      </section>
      </main>

      <LandingFooter anchorBasePath="/" />
    </div>
  );
}

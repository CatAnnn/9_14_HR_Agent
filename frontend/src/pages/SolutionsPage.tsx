import { ArrowRight } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { LandingHeader } from '../components/LandingExperience';
import { STATIC_ASSETS } from '../config/staticAssets';
import { homeExperienceContent } from '../content/home-experience-content';
import { toolkitPageList } from '../content/toolkit-content';
import { useViewportReveal } from '../hooks/useViewportReveal';
import { useLanguage } from '../i18n/LanguageContext';
import '../styles/home-page.css';
import '../styles/solutions-page.css';

export default function SolutionsPage() {
  const { translate, translateTemplate } = useLanguage();
  const pageRef = useRef<HTMLDivElement>(null);
  const directoryRef = useRef<HTMLElement>(null);
  const [directoryVisible, setDirectoryVisible] = useState(false);
  useViewportReveal(pageRef);

  useEffect(() => {
    const previousTitle = document.title;
    document.title = translate('解决方案 | Performance Feedback');

    return () => {
      document.title = previousTitle;
    };
  }, [translate]);

  useEffect(() => {
    const directory = directoryRef.current;
    if (!directory) return undefined;
    if (
      typeof IntersectionObserver === 'undefined'
      || !window.matchMedia('(min-width: 1024px) and (prefers-reduced-motion: no-preference)').matches
    ) {
      setDirectoryVisible(true);
      return undefined;
    }

    const observer = new IntersectionObserver(([entry]) => {
      if (!entry?.isIntersecting) return;
      setDirectoryVisible(true);
      observer.disconnect();
    }, { rootMargin: '0px 0px -10% 0px', threshold: 0.15 });
    observer.observe(directory);
    return () => observer.disconnect();
  }, []);

  return (
    <div ref={pageRef} className="solutions-overview-page">
      <LandingHeader
        navigation={homeExperienceContent.navigation}
        homeHref="/"
        anchorBasePath="/"
        inverse
        transparent
      />

      <main className="solutions-overview-main">
        <section className="solutions-overview-hero" aria-labelledby="solutions-overview-title">
          <img
            className="solutions-overview-hero-media"
            src="/assets/landing/solution-page-background.webp"
            alt={translate('阳光下的山峰、山谷与高山草甸')}
            width={1672}
            height={941}
            fetchPriority="high"
            decoding="async"
          />
          <span className="solutions-overview-hero-scrim" aria-hidden="true" />
          <div className="solutions-overview-inner solutions-overview-hero-inner">
            <h1 id="solutions-overview-title">{translate('解决方案')}</h1>
            <p className="solutions-overview-lead">
              {translate('将经过验证的方法与模型带入人才管理场景，帮助管理者看清问题、形成判断，并将每一次对话转化为可执行的行动。')}
            </p>
          </div>
        </section>

        <section className="solutions-overview-intro" aria-labelledby="solutions-intro-title">
          <div className="solutions-overview-inner solutions-overview-intro-inner">
            <header
              className="solutions-overview-intro-heading"
              data-viewport-reveal="headline"
            >
              <p>{translate('从洞察到行动')}</p>
              <h2 id="solutions-intro-title">{translate('成熟框架，让复杂判断更清晰')}</h2>
            </header>
            <div
              className="solutions-overview-intro-copy"
              data-viewport-reveal="rise-delayed"
            >
              <p>
                {translate('优秀的管理决策很少依赖直觉本身。成熟的方法能够帮助管理者整理信息、检验假设，并在复杂的人才议题中聚焦真正影响结果的因素。')}
              </p>
              <p>
                {translate('本页汇集战略分析、目标设定、反馈评估与人才发展的常用框架。无需一次掌握全部内容，从当前最需要解决的问题开始，选择一个方法并在真实沟通中持续应用。')}
              </p>
            </div>
          </div>
        </section>

        <section
          ref={directoryRef}
          id="solutions-directory"
          className={`solutions-overview-directory${directoryVisible ? ' is-visible' : ''}`}
          aria-labelledby="solutions-directory-title"
        >
          <div className="solutions-overview-inner">
            <header
              className="solutions-overview-heading"
              data-viewport-reveal="headline"
            >
              <h2 id="solutions-directory-title">{translate('具体提供方式')}</h2>
              <span>{translate('以四类经过实践验证的框架，将复杂判断转化为清晰、可执行的下一步行动。')}</span>
            </header>

            <div className="solutions-category-grid">
              {toolkitPageList.map((page, index) => (
                <article
                  className="solutions-category-card"
                  key={page.slug}
                  style={{ transitionDelay: `${index * 70}ms` }}
                >
                  <Link
                    className="solutions-category-media"
                    to={page.path}
                    aria-label={translateTemplate(
                      '查看 {title}',
                      'View {title}',
                      { title: translate(page.navLabel, page.navLabelEnglish) },
                    )}
                  >
                    <img src={page.heroImage} alt={translate(page.heroImageAlt)} loading="lazy" fetchPriority="low" decoding="async" />
                  </Link>
                  <div className="solutions-category-card-body">
                    <h3>
                      <Link to={page.path}>
                        <span>{translate(page.navLabel, page.navLabelEnglish)}</span>
                        <ArrowRight aria-hidden="true" />
                      </Link>
                    </h3>
                    <p>{translate(page.summary, page.summaryEnglish)}</p>
                  </div>
                </article>
              ))}
            </div>
          </div>
        </section>

        <section className="solutions-overview-cta" aria-labelledby="solutions-cta-title">
          <div className="solutions-overview-inner solutions-overview-cta-inner">
            <h2 id="solutions-cta-title" data-viewport-reveal="headline">
              {translate('将方法带入下一次管理对话')}
            </h2>
            <Link to="/app/introduction" data-viewport-reveal="rise-delayed">
              {translate('进入预演')} <ArrowRight aria-hidden="true" />
            </Link>
          </div>
        </section>
      </main>

      <footer className="solutions-overview-footer">
        <div className="solutions-overview-inner solutions-overview-footer-grid">
          <div
            className="solutions-overview-footer-brand"
            data-viewport-reveal="rise"
          >
            <Link to="/" aria-label={translate('返回首页')}>
              <img src={STATIC_ASSETS.companyLogoBlack} alt="Bosch" loading="lazy" decoding="async" />
            </Link>
            <strong>Performance Feedback</strong>
            <p>{translate('连接人才洞察、专业框架与真实管理实践。')}</p>
          </div>
          <nav
            aria-label={translate('解决方案主题')}
            data-viewport-reveal="rise-delayed"
          >
            <strong>{translate('解决方案')}</strong>
            {toolkitPageList.map((page) => (
              <Link to={page.path} key={page.slug}>
                {translate(page.navLabel, page.navLabelEnglish)}
              </Link>
            ))}
          </nav>
          <nav
            aria-label={translate('资源与工作台')}
            data-viewport-reveal="rise-late"
          >
            <strong>{translate('快速入口')}</strong>
            <Link to="/resources">{translate('资源中心')}</Link>
            <Link to="/app/introduction">{translate('沟通工作台')}</Link>
            <Link to="/">{translate('返回首页')}</Link>
          </nav>
        </div>
        <div
          className="solutions-overview-inner solutions-overview-footer-legal"
          data-viewport-reveal="rise"
        >
          <span>{translate('仅限授权用户使用')}</span>
          <span>© {new Date().getFullYear()} Bosch</span>
        </div>
      </footer>
    </div>
  );
}

import { ArrowRight } from 'lucide-react';
import { useRef } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { LandingFooter, LandingHeader } from '../components/LandingExperience';
import { homeExperienceContent } from '../content/home-experience-content';
import {
  performanceManagementDimensions,
} from '../content/performance-management-content';
import { performanceManagementDimensionPresentation } from '../content/performance-management-presentation';
import { useViewportReveal } from '../hooks/useViewportReveal';
import { useLanguage } from '../i18n/LanguageContext';
import '../styles/home-page.css';
import '../styles/resource-center-page.css';

const DIMENSION_SECTION_ID = 'performance-management-dimensions';
const RESOURCE_HERO_IMAGE = '/assets/resource/resources.webp';

export default function ResourcePage() {
  const navigate = useNavigate();
  const { translate, translateTemplate } = useLanguage();
  const pageRef = useRef<HTMLElement>(null);
  useViewportReveal(pageRef);

  return (
    <main ref={pageRef} className="resource-page">
      <LandingHeader
        navigation={homeExperienceContent.navigation}
        homeHref="/"
        anchorBasePath="/"
        inverse
        transparent
      />

      <section className="resource-page-hero" aria-labelledby="resource-page-title">
        <picture className="resource-page-hero-background" aria-hidden="true">
          <source media="(max-width: 720px)" srcSet={RESOURCE_HERO_IMAGE} />
          <img
            src={RESOURCE_HERO_IMAGE}
            alt=""
            width={1672}
            height={941}
            fetchPriority="high"
            decoding="async"
          />
        </picture>
        <div className="resource-page-hero-inner">
          <div className="resource-page-hero-copy">
            <h1 id="resource-page-title">{translate('资源中心')}</h1>
            <p>
              {translate('从理解绩效、设定目标、反馈评估，到结果管理与低绩效改进，按五个维度找到当前需要的方法与行动提示。')}
            </p>
          </div>
        </div>
      </section>

      <section className="resource-page-intro" aria-labelledby="resource-page-intro-title">
        <div className="resource-page-intro-inner">
          <header data-viewport-reveal="headline">
            <p>{translate('从评估者到赋能者')}</p>
            <h2 id="resource-page-intro-title">{translate('让绩效管理成为持续发生的管理对话')}</h2>
          </header>
          <div data-viewport-reveal="rise-delayed">
            <p>
              {translate('五个维度沿用绩效管理草稿的原始章节，不增加新的分类；每一页都将长段落转化为对比、步骤、检查项和行动提示。')}
            </p>
            <p>
              {translate('涉及奖金、PIP、合同处理等尚未定稿的政策内容会明确提示审核状态，并以正式政策及 HRBP/Legal 确认为准。')}
            </p>
          </div>
        </div>
      </section>

      <div className="resource-page-content-shell">
        <div className="resource-page-content-column">
          <section
            id={DIMENSION_SECTION_ID}
            className="resource-page-section resource-performance-section"
            aria-labelledby="performance-dimensions-title"
          >
            <header
              className="resource-section-heading resource-dimension-heading"
              data-viewport-reveal="headline"
            >
              <h2 id="performance-dimensions-title">{translate('绩效管理的五个维度')}</h2>
              <p>{translate('按顺序阅读完整路径，或直接进入当前管理任务对应的维度。')}</p>
            </header>

            <div className="resource-dimension-grid">
              {performanceManagementDimensions.map((dimension, index) => {
                const media = performanceManagementDimensionPresentation[dimension.slug];
                const module = homeExperienceContent.resourceModules.find((item) => item.id === dimension.slug);
                const localizedTitle = translate(dimension.title, dimension.englishTitle);
                const localizedSummary = translate(dimension.summary, module?.descriptionEnglish);
                return (
                  <Link
                    className="resource-dimension-card"
                    key={dimension.slug}
                    to={dimension.path}
                    data-viewport-reveal="rise"
                    data-viewport-reveal-delay={index * 55}
                    aria-label={translateTemplate(
                      '第 {index} 维：{title}，{summary}',
                      'Dimension {index}: {title}. {summary}',
                      { index: index + 1, title: localizedTitle, summary: localizedSummary },
                    )}
                  >
                    <span className="resource-dimension-media">
                      <img
                        src={media.src}
                        alt={translate(media.alt, media.altEnglish)}
                        loading={index < 3 ? 'eager' : 'lazy'}
                        decoding="async"
                      />
                    </span>
                    <span className="resource-dimension-card-copy">
                      <span className="resource-dimension-meta">
                        <span>{dimension.number}</span>
                        <span aria-hidden="true">/</span>
                        <span>{dimension.englishTitle}</span>
                      </span>
                      <span className="resource-dimension-title-row">
                        <h3>{localizedTitle}</h3>
                        <ArrowRight aria-hidden="true" />
                      </span>
                      <span className="resource-dimension-card-summary">{localizedSummary}</span>
                    </span>
                  </Link>
                );
              })}
            </div>
          </section>
        </div>
      </div>

      <section className="resource-contact-band" aria-labelledby="resource-contact-title">
        <div className="resource-contact-band-inner">
          <div data-viewport-reveal="headline">
            <p>{translate('应用到真实场景')}</p>
            <h2 id="resource-contact-title">{translate('把方法转化为下一次管理行动')}</h2>
            <span>{translate('完成阅读后，可进入沟通工作台准备谈话、整理事实并开展练习。')}</span>
          </div>
          <button
            type="button"
            data-viewport-reveal="rise-delayed"
            onClick={() => navigate('/app/introduction')}
          >
            {translate('开始准备')} <ArrowRight aria-hidden="true" />
          </button>
        </div>
      </section>

      <LandingFooter anchorBasePath="/" />
    </main>
  );
}

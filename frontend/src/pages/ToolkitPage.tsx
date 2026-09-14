import {
  AlertTriangle,
  ArrowRight,
  ArrowUpRight,
  BrainCircuit,
  BriefcaseBusiness,
  Check,
  ClipboardCheck,
  GraduationCap,
  Lightbulb,
  RefreshCw,
  Search,
  Target,
  TrendingUp,
  UsersRound,
  type LucideIcon,
} from 'lucide-react';
import { lazy, useEffect, useRef, useState, type CSSProperties } from 'react';
import { Link, useParams } from 'react-router-dom';
import { LandingHeader } from '../components/LandingExperience';
import { RouteRedirect } from '../components/RouteLoading';
import { MethodArticleShell } from '../components/toolkit/MethodArticleShell';
import { STATIC_ASSETS } from '../config/staticAssets';
import { homeExperienceContent } from '../content/home-experience-content';
import {
  getMethodArticleConfig,
  type MethodArticleConfig,
} from '../content/method-article-config';
import {
  findToolkitTool,
  isToolkitPageSlug,
  mckinseySevenSArticle,
  toolkitPageList,
  toolkitPages,
  toolkitToolVisuals,
  type ToolkitDetailItem,
  type ToolkitDetailSection,
  type ToolkitIconName,
  type ToolkitPageConfig,
  type ToolkitTool,
} from '../content/toolkit-content';
import { useViewportReveal } from '../hooks/useViewportReveal';
import { useLanguage } from '../i18n/LanguageContext';
import '../styles/home-page.css';
import '../styles/toolkit-page.css';
import '../styles/method-article.css';

const TOOLKIT_VIEWPORT_REVEAL_SELECTOR = [
  '[data-viewport-reveal]',
  '[data-home-reveal]',
  '.toolkit-custom-method-page :is(.cgo-editorial, .cgo-principles, .cgo-grow-loop, .cgo-manager, .cgo-boundaries, .cgo-culture, .cgo-tool-guide, .f360-section, .ssa-section, .sga-section, .grow-section, .belbin-section, .lsa-section, .wsa-section, section[id^="smart-"]) > :first-child',
].join(', ');

const BelbinTeamRolesArticle = lazy(() => import('../components/toolkit/BelbinTeamRolesArticle')
  .then((module) => ({ default: module.BelbinTeamRolesArticle })));
const CoachingGoalsOverview = lazy(() => import('../components/toolkit/CoachingGoalsOverview')
  .then((module) => ({ default: module.CoachingGoalsOverview })));
const GrowModelArticle = lazy(() => import('../components/toolkit/GrowModelArticle')
  .then((module) => ({ default: module.GrowModelArticle })));
const LeadershipSelfAssessmentArticle = lazy(() => import('../components/toolkit/LeadershipSelfAssessmentArticle')
  .then((module) => ({ default: module.LeadershipSelfAssessmentArticle })));
const SkillsGapAnalysisArticle = lazy(() => import('../components/toolkit/SkillsGapAnalysisArticle')
  .then((module) => ({ default: module.SkillsGapAnalysisArticle })));
const SmartGoalsArticle = lazy(() => import('../components/toolkit/SmartGoalsArticle')
  .then((module) => ({ default: module.SmartGoalsArticle })));
const SoftSkillsAssessmentArticle = lazy(() => import('../components/toolkit/SoftSkillsAssessmentArticle')
  .then((module) => ({ default: module.SoftSkillsAssessmentArticle })));
const ThreeSixtyFeedbackArticle = lazy(() => import('../components/toolkit/ThreeSixtyFeedbackArticle')
  .then((module) => ({ default: module.ThreeSixtyFeedbackArticle })));
const WorkplaceSkillsAssessmentArticle = lazy(() => import('../components/toolkit/WorkplaceSkillsAssessmentArticle')
  .then((module) => ({ default: module.WorkplaceSkillsAssessmentArticle })));

const toolkitIcons: Readonly<Record<ToolkitIconName, LucideIcon>> = {
  strategy: BriefcaseBusiness,
  analysis: Search,
  alignment: RefreshCw,
  skills: GraduationCap,
  goal: Target,
  coaching: UsersRound,
  feedback: UsersRound,
  assessment: ClipboardCheck,
  team: UsersRound,
  leadership: TrendingUp,
  development: BrainCircuit,
};

const ARTICLE_META_ENGLISH: Readonly<Record<string, string>> = {
  '组织 · 团队 · 个人': 'Organization · Team · Individual',
  '实践版 · 2026': 'Practice edition · 2026',
};

const TOOLKIT_CTA_ENGLISH: Readonly<Record<ToolkitPageConfig['slug'], { title: string; description: string }>> = {
  'strategy-analysis': {
    title: 'Bring analysis into real conversations',
    description: 'Organize employee context and conversation intent in the workspace, then turn analysis into actionable manager dialogue.',
  },
  'coaching-goals': {
    title: 'Practice before the real coaching conversation',
    description: 'Use preparation guidance and multi-turn rehearsal to test question order, goal clarity, and likely employee responses.',
  },
  'feedback-assessment': {
    title: 'Turn assessment results into high-quality feedback',
    description: 'Practice stating facts, acknowledging emotion, and shaping an improvement plan together with the employee.',
  },
  'development-frameworks': {
    title: 'Prepare for the next development conversation',
    description: 'Bring role expectations, employee motives, and current performance into one conversation, then rehearse to test the message.',
  },
};

function localizedReadingTime(
  readingTime: string,
  translateTemplate: ReturnType<typeof useLanguage>['translateTemplate'],
): string {
  const minutes = readingTime.match(/约\s*(\d+)\s*分钟阅读/u)?.[1];
  return minutes
    ? translateTemplate('约 {minutes} 分钟阅读', 'About {minutes} min read', { minutes })
    : readingTime;
}

function renderCustomArticle(toolId: string) {
  switch (toolId) {
    case 'skills-gap-analysis':
      return <SkillsGapAnalysisArticle />;
    case 'grow-model':
      return <GrowModelArticle />;
    case 'smart-goals':
      return <SmartGoalsArticle />;
    case '360-degree-feedback':
      return <ThreeSixtyFeedbackArticle />;
    case 'leadership-self-assessment':
      return <LeadershipSelfAssessmentArticle />;
    case 'belbin-team-roles':
      return <BelbinTeamRolesArticle />;
    case 'soft-skills-assessment':
      return <SoftSkillsAssessmentArticle />;
    case 'workplace-skills-assessment':
      return <WorkplaceSkillsAssessmentArticle />;
    default:
      return null;
  }
}

export default function ToolkitPage() {
  const { toolkitSlug, toolSlug } = useParams<{ toolkitSlug: string; toolSlug?: string }>();
  const { translate, translateTemplate } = useLanguage();
  const page = isToolkitPageSlug(toolkitSlug) ? toolkitPages[toolkitSlug] : null;
  const tool = page ? findToolkitTool(page, toolSlug) : undefined;
  const pageRef = useRef<HTMLDivElement>(null);
  useViewportReveal(pageRef, {
    resetKey: `${toolkitSlug ?? ''}:${toolSlug ?? ''}`,
    selector: TOOLKIT_VIEWPORT_REVEAL_SELECTOR,
  });

  useEffect(() => {
    if (!page || (toolSlug && !tool)) return;
    document.title = translateTemplate(
      '{title} | Performance Feedback',
      '{title} | Performance Feedback',
      { title: tool?.title ?? translate(page.navLabel, page.navLabelEnglish) },
    );
  }, [page, tool, toolSlug, translate, translateTemplate]);

  if (!page) return <RouteRedirect to="/" />;
  if (toolSlug && !tool) return <RouteRedirect to={page.path} />;

  const theme = {
    '--toolkit-accent': page.accent,
    '--toolkit-accent-soft': page.accentSoft,
  } as CSSProperties;

  return (
    <div ref={pageRef} className="toolkit-page" style={theme}>
      <ToolkitHeader inverse={Boolean(tool)} />
      {tool ? <MethodPage page={page} tool={tool} /> : <CategoryPage page={page} />}
      <ToolkitFooter />
    </div>
  );
}

function ToolkitHeader({ inverse = false }: { inverse?: boolean }) {
  const { translate } = useLanguage();
  return (
    <LandingHeader
      navigation={homeExperienceContent.navigation}
      homeHref="/solutions"
      homeAriaLabel={translate('返回解决方案主页', 'Back to the Solutions homepage')}
      anchorBasePath="/"
      inverse={inverse}
      transparent
    />
  );
}

function CategoryPage({ page }: { page: ToolkitPageConfig }) {
  const hasCoachingGoalsOverview = page.slug === 'coaching-goals';

  return (
    <main>
      <ToolkitHero page={page} />

      {hasCoachingGoalsOverview ? (
        <CoachingGoalsOverview />
      ) : (
        <section className="toolkit-introduction" aria-labelledby="toolkit-intro-title">
          <div
            className="toolkit-page-inner toolkit-introduction-inner"
            data-viewport-reveal="rise"
          >
            <h2 id="toolkit-intro-title">{page.introTitle}</h2>
            {page.introParagraphs.map((paragraph) => <p key={paragraph}>{paragraph}</p>)}
          </div>
        </section>
      )}

      <ToolDirectory page={page} />
      <ToolkitCta page={page} />
    </main>
  );
}

function ToolkitHero({
  page,
  tool,
  articleConfig,
}: {
  page: ToolkitPageConfig;
  tool?: ToolkitTool;
  articleConfig?: MethodArticleConfig;
}) {
  const { translate, translateTemplate } = useLanguage();
  const Icon = toolkitIcons[tool?.icon ?? page.tools[0].icon];
  const visual = tool ? toolkitToolVisuals[tool.id] : undefined;
  const title = tool?.title ?? translate(page.title, page.titleEnglish);
  const summary = tool?.detail.subtitle ?? translate(page.summary, page.summaryEnglish);
  const image = visual?.image ?? page.heroImage;
  const imageAlt = translate(visual?.imageAlt ?? page.heroImageAlt);

  if (tool) {
    return (
      <section className="toolkit-hero toolkit-method-hero" aria-labelledby="toolkit-page-title">
        <figure
          className="toolkit-method-hero-media"
          style={{
            '--toolkit-method-image-position-mobile': visual?.mobileObjectPosition ?? 'center',
          } as CSSProperties}
        >
          <img src={image} alt={imageAlt} fetchPriority="high" />
        </figure>
        <span className="toolkit-method-hero-scrim" aria-hidden="true" />
        <div className="toolkit-page-inner toolkit-method-hero-inner">
          <div className="toolkit-hero-copy">
            <Link className="toolkit-hero-eyebrow" to={page.path}>
              <Icon aria-hidden="true" /> {translate(page.navLabel, page.navLabelEnglish)}
            </Link>
            <h1 id="toolkit-page-title">{title}</h1>
            <p>{summary}</p>
            <div
              className="toolkit-method-hero-meta"
              aria-label={translate('文章信息', 'Article information')}
            >
              <span>{articleConfig?.readingTime
                ? localizedReadingTime(articleConfig.readingTime, translateTemplate)
                : translate('方法指南', 'Method guide')}</span>
              {articleConfig?.meta?.map((item) => (
                <span key={item}>{translate(item, ARTICLE_META_ENGLISH[item])}</span>
              ))}
            </div>
          </div>
        </div>
      </section>
    );
  }

  return (
    <section className="toolkit-hero" aria-labelledby="toolkit-page-title">
      <div className="toolkit-page-inner toolkit-hero-inner">
        <div className="toolkit-hero-copy">
          <h1 id="toolkit-page-title">{title}</h1>
          <p>{summary}</p>
        </div>
        <figure className="toolkit-hero-media">
          <img src={image} alt={imageAlt} fetchPriority="high" />
        </figure>
      </div>
    </section>
  );
}

function ToolDirectory({ page }: { page: ToolkitPageConfig }) {
  const { translate } = useLanguage();
  const sectionRef = useRef<HTMLElement>(null);
  const [isVisible, setIsVisible] = useState(false);

  useEffect(() => {
    const section = sectionRef.current;
    if (!section) return undefined;

    setIsVisible(false);
    if (
      typeof IntersectionObserver === 'undefined'
      || !window.matchMedia('(min-width: 1024px) and (prefers-reduced-motion: no-preference)').matches
    ) {
      setIsVisible(true);
      return undefined;
    }

    const observer = new IntersectionObserver(([entry]) => {
      if (!entry?.isIntersecting) return;
      setIsVisible(true);
      observer.disconnect();
    }, { rootMargin: '0px 0px -10% 0px', threshold: 0.12 });

    observer.observe(section);
    return () => observer.disconnect();
  }, [page.slug]);

  return (
    <section
      ref={sectionRef}
      id="toolkit-tools"
      className={`toolkit-tools${isVisible ? ' is-visible' : ''}`}
      aria-labelledby="toolkit-tools-title"
    >
      <div className="toolkit-page-inner">
        <header className="toolkit-section-heading" data-viewport-reveal="headline">
          <h2 id="toolkit-tools-title">{translate(page.toolsHeading, page.toolsHeadingEnglish)}</h2>
          <p>{translate(
            '选择适合当前管理议题的方法与工具，进入完整框架、应用步骤和实践说明。',
            'Choose a method or tool for the current management challenge, then explore its full framework, application steps, and practice guidance.',
          )}</p>
        </header>

        <div className="toolkit-tool-track">
          {page.tools.map((tool, index) => (
            <ToolDirectoryCard tool={tool} index={index} key={tool.id} />
          ))}
        </div>
      </div>
    </section>
  );
}

function ToolDirectoryCard({ tool, index }: { tool: ToolkitTool; index: number }) {
  const { translate } = useLanguage();
  const visual = toolkitToolVisuals[tool.id];
  return (
    <Link
      className="toolkit-tool-card"
      to={tool.path}
      style={{ transitionDelay: `${index * 70}ms` }}
    >
      <figure>
        <img src={visual.image} alt={translate(visual.imageAlt)} loading="lazy" decoding="async" />
      </figure>
      <div>
        <h3><span>{tool.title}</span><ArrowRight aria-hidden="true" /></h3>
        <p>{translate(tool.summary, tool.summaryEnglish)}</p>
      </div>
    </Link>
  );
}

function MethodPage({ page, tool }: { page: ToolkitPageConfig; tool: ToolkitTool }) {
  const relatedTools = page.tools.filter((item) => item.id !== tool.id).slice(0, 3);
  const articleConfig = getMethodArticleConfig(tool.id);
  const customArticle = renderCustomArticle(tool.id);
  const methodClassName = [
    'toolkit-method-page',
    tool.id === 'swot-analysis' ? 'toolkit-swot-page' : '',
    tool.id === 'vrio-analysis' ? 'toolkit-vrio-page' : '',
    tool.id === 'mckinsey-7s' ? 'toolkit-seven-s-page' : '',
    customArticle ? 'toolkit-custom-method-page' : '',
  ].filter(Boolean).join(' ');

  return (
    <main className={methodClassName}>
      <MethodArticleShell
        methodId={tool.id}
        title={tool.title}
        sections={articleConfig.sections}
        hero={<ToolkitHero page={page} tool={tool} articleConfig={articleConfig} />}
      >
        {customArticle ?? <GenericMethodArticle tool={tool} />}
      </MethodArticleShell>
      <ToolkitRelatedSection page={page} tools={relatedTools} />
      <ToolkitCta page={page} />
    </main>
  );
}

function GenericMethodArticle({ tool }: { tool: ToolkitTool }) {
  const Icon = toolkitIcons[tool.icon];
  const isSwot = tool.id === 'swot-analysis';
  const isVrio = tool.id === 'vrio-analysis';
  const isSevenS = tool.id === 'mckinsey-7s';

  return (
    <>
      <section
        id="method-overview"
        className={`toolkit-method-overview${isSwot ? ' toolkit-swot-overview' : ''}${isVrio ? ' toolkit-vrio-overview' : ''}${isSevenS ? ' toolkit-seven-s-overview' : ''}`}
        aria-labelledby="method-overview-title"
      >
        <div className="toolkit-page-inner" data-viewport-reveal="rise">
          <span className="toolkit-article-kicker"><Icon aria-hidden="true" /> Framework overview</span>
          <h2 id="method-overview-title">{tool.detail.overviewTitle}</h2>
          {tool.detail.overview.map((paragraph) => <p key={paragraph}>{paragraph}</p>)}
        </div>
      </section>

      <section id="method-framework" className="toolkit-method-framework" aria-labelledby="method-framework-title">
        <div className="toolkit-page-inner" data-viewport-reveal="rise">
          <span className="toolkit-article-kicker"><Lightbulb aria-hidden="true" /> Core framework</span>
          <h2 id="method-framework-title">{tool.detail.frameworkTitle}</h2>
          {isSwot ? (
            <SwotMatrix items={tool.detail.framework} label="SWOT 四象限构成" />
          ) : isVrio ? (
            <VrioSequence items={tool.detail.framework} />
          ) : isSevenS ? (
            <SevenSFramework items={tool.detail.framework} />
          ) : (
            <div className="toolkit-framework-grid">
              {tool.detail.framework.map((item, index) => (
                <article key={item.title}>
                  <small>{String(index + 1).padStart(2, '0')}</small>
                  <h3>{item.title}</h3>
                  <p>{item.detail}</p>
                </article>
              ))}
            </div>
          )}
        </div>
      </section>

      {isSwot && tool.detail.exampleSection && (
        <SwotExampleSection section={tool.detail.exampleSection} />
      )}
      {isVrio && tool.detail.exampleSection && (
        <VrioExampleSection section={tool.detail.exampleSection} />
      )}
      {isSevenS && <SevenSDiagnosticsSection />}
      {isSevenS && <SevenSCaseStudy />}

      <section id="method-process" className="toolkit-method-process" aria-labelledby="method-process-title">
        <div className="toolkit-page-inner" data-viewport-reveal="rise">
          <span className="toolkit-article-kicker"><RefreshCw aria-hidden="true" /> Step by step</span>
          <h2 id="method-process-title">{tool.detail.processTitle}</h2>
          <ol className="toolkit-process-list">
            {tool.detail.process.map((item, index) => (
              <li key={item.title}>
                <span>{String(index + 1).padStart(2, '0')}</span>
                <div><h3>{item.title}</h3><p>{item.detail}</p></div>
              </li>
            ))}
          </ol>
        </div>
      </section>

      {isSwot && tool.detail.benefitsSection && (
        <SwotBenefitsSection section={tool.detail.benefitsSection} />
      )}
      {isVrio && tool.detail.benefitsSection && (
        <VrioOutcomesSection section={tool.detail.benefitsSection} />
      )}
      {isSevenS && <SevenSComparisonSection />}

      <section className="toolkit-method-evidence">
        <div className="toolkit-page-inner toolkit-method-evidence-grid">
          <article
            id="method-application"
            aria-labelledby="method-application-title"
            data-viewport-reveal="rise"
          >
            <span className="toolkit-article-kicker"><Check aria-hidden="true" /> When to use</span>
            <h2 id="method-application-title">什么时候适合使用？</h2>
            <ul className="toolkit-use-list">
              {tool.detail.useCases.map((item) => <li key={item}><Check aria-hidden="true" /><span>{item}</span></li>)}
            </ul>
          </article>

          <article
            id="method-pitfalls"
            aria-labelledby="method-pitfalls-title"
            data-viewport-reveal="rise-delayed"
          >
            <span className="toolkit-article-kicker"><AlertTriangle aria-hidden="true" /> Common mistakes</span>
            <h2 id="method-pitfalls-title">常见误区与修正方式</h2>
            <div className="toolkit-pitfall-list">
              {tool.detail.pitfalls.map((item) => (
                <div key={item.title}>
                  <AlertTriangle aria-hidden="true" />
                  <div><h3>{item.title}</h3><p>{item.detail}</p></div>
                </div>
              ))}
            </div>
          </article>
        </div>
      </section>

      {isSevenS && <SevenSPresentationSection />}

      <section id="method-manager" className="toolkit-manager-section" aria-labelledby="method-manager-title">
        <div className="toolkit-page-inner toolkit-manager-inner">
          <div data-viewport-reveal="headline">
            <span className="toolkit-article-kicker"><UsersRound aria-hidden="true" /> Manager application</span>
            <h2 id="method-manager-title">在经理沟通中如何应用</h2>
          </div>
          <div data-viewport-reveal="rise-delayed">
            {tool.detail.managerApplication.map((paragraph) => <p key={paragraph}>{paragraph}</p>)}
            <blockquote>
              <strong>关键结论</strong>
              <p>{tool.detail.keyTakeaway}</p>
            </blockquote>
          </div>
        </div>
      </section>
    </>
  );
}

function ToolkitRelatedSection({
  page,
  tools,
}: {
  page: ToolkitPageConfig;
  tools: readonly ToolkitTool[];
}) {
  const { translate, translateTemplate } = useLanguage();
  return (
    <section className="toolkit-related" aria-labelledby="toolkit-related-title">
      <div className="toolkit-page-inner">
        <header className="toolkit-section-heading" data-viewport-reveal="headline">
          <div>
            <p>{translate('相关内容', 'Related content')}</p>
            <h2 id="toolkit-related-title">{translateTemplate(
              '继续探索 {title}',
              'Continue exploring {title}',
              { title: translate(page.navLabel, page.navLabelEnglish) },
            )}</h2>
          </div>
        </header>
        <div className="toolkit-related-grid">
          {tools.map((item, index) => {
            const RelatedIcon = toolkitIcons[item.icon];
            return (
              <Link
                to={item.path}
                key={item.id}
                data-viewport-reveal="rise"
                data-viewport-reveal-delay={index * 70}
              >
                <RelatedIcon aria-hidden="true" />
                <div>
                  <strong>{item.title}</strong>
                  <span>{translate(item.summary, item.summaryEnglish)}</span>
                </div>
                <ArrowUpRight aria-hidden="true" />
              </Link>
            );
          })}
        </div>
      </div>
    </section>
  );
}

const swotQuadrantMeta = [
  { letter: 'S', axis: '内部因素 · 有利', className: 'is-strength' },
  { letter: 'W', axis: '内部因素 · 不利', className: 'is-weakness' },
  { letter: 'O', axis: '外部因素 · 有利', className: 'is-opportunity' },
  { letter: 'T', axis: '外部因素 · 不利', className: 'is-threat' },
] as const;

function SwotMatrix({
  items,
  label,
  example = false,
}: {
  items: readonly ToolkitDetailItem[];
  label: string;
  example?: boolean;
}) {
  return (
    <div
      className={`toolkit-swot-matrix${example ? ' is-example' : ''}`}
      role="group"
      aria-label={label}
    >
      <span className="toolkit-swot-matrix-corner" aria-hidden="true">影响来源</span>
      <span className="toolkit-swot-column-label is-helpful" aria-hidden="true">有利因素</span>
      <span className="toolkit-swot-column-label is-harmful" aria-hidden="true">不利因素</span>
      <span className="toolkit-swot-row-label is-internal" aria-hidden="true">内部因素</span>
      <span className="toolkit-swot-row-label is-external" aria-hidden="true">外部因素</span>
      {items.slice(0, 4).map((item, index) => {
        const meta = swotQuadrantMeta[index] ?? swotQuadrantMeta[0];
        return (
          <article className={meta.className} key={item.title}>
            <header>
              <b aria-hidden="true">{meta.letter}</b>
              <div>
                <small>{meta.axis}</small>
                <h3>{item.title}</h3>
              </div>
            </header>
            <p>{item.detail}</p>
          </article>
        );
      })}
    </div>
  );
}

function SwotExampleSection({ section }: { section: ToolkitDetailSection }) {
  return (
    <section id="method-example" className="toolkit-swot-example" aria-labelledby="method-example-title">
      <div className="toolkit-page-inner">
        <header className="toolkit-swot-section-heading">
          <span className="toolkit-article-kicker"><Search aria-hidden="true" /> Worked example</span>
          <h2 id="method-example-title">{section.title}</h2>
          <p>{section.description}</p>
        </header>
        <SwotMatrix items={section.items} label={section.title} example />
      </div>
    </section>
  );
}

function SwotBenefitsSection({ section }: { section: ToolkitDetailSection }) {
  return (
    <section id="method-benefits" className="toolkit-swot-benefits" aria-labelledby="method-benefits-title">
      <div className="toolkit-page-inner">
        <header className="toolkit-swot-section-heading">
          <span className="toolkit-article-kicker"><TrendingUp aria-hidden="true" /> Benefits</span>
          <h2 id="method-benefits-title">{section.title}</h2>
          <p>{section.description}</p>
        </header>
        <div className="toolkit-swot-benefit-grid">
          {section.items.map((item, index) => (
            <article key={item.title}>
              <span>{String(index + 1).padStart(2, '0')}</span>
              <h3>{item.title}</h3>
              <p>{item.detail}</p>
            </article>
          ))}
        </div>
      </div>
    </section>
  );
}

const vrioCriterionMeta = [
  { letter: 'V', question: '是否创造净价值？', className: 'is-value' },
  { letter: 'R', question: '是否足够稀缺？', className: 'is-rarity' },
  { letter: 'I', question: '是否难以复制？', className: 'is-imitability' },
  { letter: 'O', question: '组织是否准备好？', className: 'is-organization' },
] as const;

const vrioOutcomeMeta = [
  { answers: ['否', '—', '—', '—'], className: 'is-disadvantage' },
  { answers: ['是', '否', '—', '—'], className: 'is-parity' },
  { answers: ['是', '是', '否', '—'], className: 'is-temporary' },
  { answers: ['是', '是', '是', '否'], className: 'is-unrealized' },
  { answers: ['是', '是', '是', '是'], className: 'is-sustained' },
] as const;

function VrioSequence({ items }: { items: readonly ToolkitDetailItem[] }) {
  return (
    <div className="toolkit-vrio-sequence" role="list" aria-label="VRIO 四项连续检验">
      {items.slice(0, 4).map((item, index) => {
        const meta = vrioCriterionMeta[index] ?? vrioCriterionMeta[0];
        return (
          <article className={meta.className} role="listitem" key={item.title}>
            <header>
              <b aria-hidden="true">{meta.letter}</b>
              <div>
                <small>{String(index + 1).padStart(2, '0')} · {meta.question}</small>
                <h3>{item.title}</h3>
              </div>
            </header>
            <p>{item.detail}</p>
            {index < 3 && (
              <span className="toolkit-vrio-sequence-arrow" aria-hidden="true">
                <ArrowRight />
              </span>
            )}
          </article>
        );
      })}
    </div>
  );
}

function VrioExampleSection({ section }: { section: ToolkitDetailSection }) {
  return (
    <section id="method-example" className="toolkit-vrio-example" aria-labelledby="method-example-title">
      <div className="toolkit-page-inner">
        <header className="toolkit-vrio-section-heading">
          <span className="toolkit-article-kicker"><Search aria-hidden="true" /> Evidence-based example</span>
          <h2 id="method-example-title">{section.title}</h2>
          <p>{section.description}</p>
        </header>
        <div className="toolkit-vrio-example-grid">
          {section.items.slice(0, 4).map((item, index) => {
            const meta = vrioCriterionMeta[index] ?? vrioCriterionMeta[0];
            return (
              <article className={meta.className} key={item.title}>
                <header>
                  <b aria-hidden="true">{meta.letter}</b>
                  <div>
                    <small>判定证据 {String(index + 1).padStart(2, '0')}</small>
                    <h3>{item.title}</h3>
                  </div>
                </header>
                <p>{item.detail}</p>
              </article>
            );
          })}
        </div>
      </div>
    </section>
  );
}

function VrioOutcomesSection({ section }: { section: ToolkitDetailSection }) {
  const criterionLabels = ['价值 V', '稀缺 R', '模仿 I', '组织 O'] as const;

  return (
    <section id="method-outcomes" className="toolkit-vrio-outcomes-section" aria-labelledby="method-outcomes-title">
      <div className="toolkit-page-inner">
        <header className="toolkit-vrio-section-heading">
          <span className="toolkit-article-kicker"><TrendingUp aria-hidden="true" /> Strategic outcomes</span>
          <h2 id="method-outcomes-title">{section.title}</h2>
          <p>{section.description}</p>
        </header>
        <div className="toolkit-vrio-outcomes" role="table" aria-label="VRIO 判断结果矩阵">
          <div className="toolkit-vrio-outcome-header" role="row">
            {criterionLabels.map((label) => <span role="columnheader" key={label}>{label}</span>)}
            <span role="columnheader">竞争结果与行动</span>
          </div>
          {section.items.slice(0, 5).map((item, index) => {
            const meta = vrioOutcomeMeta[index] ?? vrioOutcomeMeta[0];
            return (
              <article className={meta.className} role="row" key={item.title}>
                {meta.answers.map((answer, answerIndex) => (
                  <span
                    className={answer === '是' ? 'is-yes' : answer === '否' ? 'is-no' : 'is-na'}
                    data-label={criterionLabels[answerIndex]}
                    role="cell"
                    aria-label={`${criterionLabels[answerIndex]}：${answer}`}
                    key={`${item.title}-${criterionLabels[answerIndex]}`}
                  >
                    {answer}
                  </span>
                ))}
                <div role="cell">
                  <small>结果 {String(index + 1).padStart(2, '0')}</small>
                  <h3>{item.title}</h3>
                  <p>{item.detail}</p>
                </div>
              </article>
            );
          })}
        </div>
      </div>
    </section>
  );
}

const sevenSElementMeta = [
  { number: '01', category: '硬要素', className: 'is-hard' },
  { number: '02', category: '硬要素', className: 'is-hard' },
  { number: '03', category: '硬要素', className: 'is-hard' },
  { number: '04', category: '核心软要素', className: 'is-core' },
  { number: '05', category: '软要素', className: 'is-soft' },
  { number: '06', category: '软要素', className: 'is-soft' },
  { number: '07', category: '软要素', className: 'is-soft' },
] as const;

function SevenSFramework({ items }: { items: readonly ToolkitDetailItem[] }) {
  const hardElements = items.slice(0, 3);
  const sharedValues = items[3];
  const softElements = items.slice(4, 7);
  if (!sharedValues) return null;

  return (
    <div className="toolkit-seven-s-framework">
      <div className="toolkit-seven-s-legend" aria-label="7S 要素分类">
        <span className="is-hard"><i aria-hidden="true" />硬要素 · 易于定义和直接调整</span>
        <span className="is-soft"><i aria-hidden="true" />软要素 · 由文化与行为塑造</span>
      </div>

      <div className="toolkit-seven-s-canvas" role="img" aria-label="Shared Values 位于中心，连接 Strategy、Structure、Systems、Skills、Style 和 Staff">
        <div className="toolkit-seven-s-stack is-hard" aria-hidden="true">
          {hardElements.map((item, index) => (
            <article key={item.title}>
              <small>{sevenSElementMeta[index].number} · Hard</small>
              <h3>{item.title}</h3>
            </article>
          ))}
        </div>

        <article className="toolkit-seven-s-core" aria-hidden="true">
          <small>At the center</small>
          <h3>{sharedValues.title}</h3>
          <p>真实信念与文化规范连接其余六个要素</p>
        </article>

        <div className="toolkit-seven-s-stack is-soft" aria-hidden="true">
          {softElements.map((item, index) => (
            <article key={item.title}>
              <small>{sevenSElementMeta[index + 4].number} · Soft</small>
              <h3>{item.title}</h3>
            </article>
          ))}
        </div>
      </div>

      <div className="toolkit-seven-s-element-groups">
        <section aria-labelledby="seven-s-hard-title">
          <header>
            <span>Hard elements · 3</span>
            <h3 id="seven-s-hard-title">可以被正式设计与直接管理</h3>
          </header>
          {hardElements.map((item, index) => (
            <article key={item.title}>
              <small>{sevenSElementMeta[index].number}</small>
              <div><h4>{item.title}</h4><p>{item.detail}</p></div>
            </article>
          ))}
        </section>

        <section aria-labelledby="seven-s-soft-title">
          <header>
            <span>Soft elements · 4</span>
            <h3 id="seven-s-soft-title">更难量化，却持续影响真实行为</h3>
          </header>
          {[sharedValues, ...softElements].map((item, index) => (
            <article key={item.title}>
              <small>{sevenSElementMeta[index + 3].number}</small>
              <div><h4>{item.title}</h4><p>{item.detail}</p></div>
            </article>
          ))}
        </section>
      </div>
    </div>
  );
}

function SevenSDiagnosticsSection() {
  const { diagnostics } = mckinseySevenSArticle;
  return (
    <section id="method-diagnostics" className="toolkit-seven-s-diagnostics" aria-labelledby="method-diagnostics-title">
      <div className="toolkit-page-inner toolkit-seven-s-section-inner">
        <header className="toolkit-seven-s-section-heading">
          <span className="toolkit-article-kicker"><Search aria-hidden="true" /> Diagnostic questions</span>
          <h2 id="method-diagnostics-title">{diagnostics.title}</h2>
          <p>{diagnostics.description}</p>
        </header>
        <div className="toolkit-seven-s-table-wrap">
          <table className="toolkit-seven-s-diagnostic-table">
            <thead><tr><th>组织要素</th><th>关键诊断问题</th></tr></thead>
            <tbody>
              {diagnostics.items.map((item) => {
                const categoryLabel = item.category === 'hard'
                  ? '硬要素'
                  : item.category === 'core' ? '核心软要素' : '软要素';
                return (
                  <tr key={item.element}>
                    <th scope="row">
                      <span className={`is-${item.category}`}>{categoryLabel}</span>
                      <strong>{item.element}</strong>
                      <small>{item.label}</small>
                    </th>
                    <td data-label="关键诊断问题">
                      <ul>{item.questions.map((question) => <li key={question}>{question}</li>)}</ul>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        <p className="toolkit-seven-s-scoring-note">
          <strong>建议：</strong>先按 1-5 分记录当前状态与目标状态，再逐对检查七个要素之间的一致、部分一致或冲突关系。
        </p>
      </div>
    </section>
  );
}

function SevenSCaseStudy() {
  const { caseStudy } = mckinseySevenSArticle;
  return (
    <section id="method-example" className="toolkit-seven-s-case" aria-labelledby="method-example-title">
      <div className="toolkit-page-inner toolkit-seven-s-section-inner">
        <header className="toolkit-seven-s-section-heading">
          <span className="toolkit-article-kicker"><BriefcaseBusiness aria-hidden="true" /> Worked example</span>
          <h2 id="method-example-title">{caseStudy.title}</h2>
          <p>{caseStudy.description}</p>
        </header>
        <div className="toolkit-seven-s-table-wrap">
          <table className="toolkit-seven-s-case-table">
            <thead>
              <tr><th>要素</th><th>工业收购方</th><th>科技公司</th><th>关键错位</th></tr>
            </thead>
            <tbody>
              {caseStudy.rows.map((row) => (
                <tr key={row.element}>
                  <th scope="row">{row.element}</th>
                  <td data-label="工业收购方">{row.acquirer}</td>
                  <td data-label="科技公司">{row.target}</td>
                  <td data-label="关键错位">{row.misalignment}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <blockquote><strong>诊断结论</strong><p>{caseStudy.insight}</p></blockquote>
        <div className="toolkit-seven-s-action-sequence">
          <h3>基于 7S 的调整顺序</h3>
          <ol>
            {caseStudy.actions.map((action, index) => (
              <li key={action}><span>{String(index + 1).padStart(2, '0')}</span><p>{action}</p></li>
            ))}
          </ol>
        </div>
      </div>
    </section>
  );
}

function SevenSComparisonSection() {
  const { comparison } = mckinseySevenSArticle;
  return (
    <section id="method-comparison" className="toolkit-seven-s-comparison" aria-labelledby="method-comparison-title">
      <div className="toolkit-page-inner toolkit-seven-s-section-inner">
        <header className="toolkit-seven-s-section-heading">
          <span className="toolkit-article-kicker"><RefreshCw aria-hidden="true" /> Model comparison</span>
          <h2 id="method-comparison-title">{comparison.title}</h2>
          <p>{comparison.description}</p>
        </header>
        <div className="toolkit-seven-s-table-wrap">
          <table className="toolkit-seven-s-comparison-table">
            <thead><tr><th>模型</th><th>关注重点</th><th>分析层级</th><th>更适合的场景</th></tr></thead>
            <tbody>
              {comparison.rows.map((row) => (
                <tr key={row.model}>
                  <th scope="row">{row.model}</th>
                  <td data-label="关注重点">{row.focus}</td>
                  <td data-label="分析层级">{row.level}</td>
                  <td data-label="更适合的场景">{row.bestFor}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}

function SevenSPresentationSection() {
  const { presentation } = mckinseySevenSArticle;
  return (
    <section id="method-presentation" className="toolkit-seven-s-presentation" aria-labelledby="method-presentation-title">
      <div className="toolkit-page-inner toolkit-seven-s-section-inner">
        <header className="toolkit-seven-s-section-heading">
          <span className="toolkit-article-kicker"><ClipboardCheck aria-hidden="true" /> Presenting the diagnosis</span>
          <h2 id="method-presentation-title">{presentation.title}</h2>
          <p>{presentation.description}</p>
        </header>
        <ol>
          {presentation.items.map((item, index) => (
            <li key={item.title}>
              <span>{String(index + 1).padStart(2, '0')}</span>
              <div><h3>{item.title}</h3><p>{item.detail}</p></div>
            </li>
          ))}
        </ol>
      </div>
    </section>
  );
}

function ToolkitCta({ page }: { page: ToolkitPageConfig }) {
  const { translate } = useLanguage();
  const english = TOOLKIT_CTA_ENGLISH[page.slug];
  return (
    <section className="toolkit-cta" aria-labelledby="toolkit-cta-title">
      <div className="toolkit-page-inner toolkit-cta-inner">
        <div data-viewport-reveal="headline">
          <p>{translate('在场景中练习', 'Practice in context')}</p>
          <h2 id="toolkit-cta-title">{translate(page.ctaTitle, english.title)}</h2>
          <span>{translate(page.ctaDescription, english.description)}</span>
        </div>
        <Link to="/app/introduction" data-viewport-reveal="rise-delayed">
          {translate('进入沟通工作台', 'Open conversation workspace')} <ArrowRight aria-hidden="true" />
        </Link>
      </div>
    </section>
  );
}

function ToolkitFooter() {
  const { translate } = useLanguage();
  return (
    <footer className="toolkit-footer">
      <div className="toolkit-page-inner toolkit-footer-grid">
        <div className="toolkit-footer-brand" data-viewport-reveal="rise">
          <Link to="/" aria-label={translate('返回概览', 'Back to Overview')}><img src={STATIC_ASSETS.companyLogoBlack} alt="Bosch" /></Link>
          <p>{translate(
            '面向经理沟通、反馈与人才发展的结构化工具库。',
            'A structured toolkit for manager conversations, feedback, and talent development.',
          )}</p>
        </div>
        <nav aria-label={translate('Toolkit 页面', 'Toolkit pages')} data-viewport-reveal="rise-delayed">
          <strong>{translate('解决方案', 'Solutions')}</strong>
          {toolkitPageList.map((page) => (
            <Link to={page.path} key={page.slug}>
              {translate(page.navLabel, page.navLabelEnglish)}
            </Link>
          ))}
        </nav>
        <nav aria-label={translate('资源中心页面', 'Resource-center pages')} data-viewport-reveal="rise-late">
          <strong>{translate('资源中心', 'Resources')}</strong>
          <Link to="/resources">{translate('绩效管理', 'Performance management')}</Link>
        </nav>
        <nav
          aria-label={translate('工作台入口', 'Workspace entry')}
          data-viewport-reveal="rise"
          data-viewport-reveal-delay={180}
        >
          <strong>Conversation Coach</strong>
          <Link to="/app/introduction">{translate('进入工作台', 'Open workspace')}</Link>
          <Link to="/">{translate('概览', 'Overview')}</Link>
        </nav>
      </div>
      <div className="toolkit-page-inner toolkit-footer-legal" data-viewport-reveal="rise">
        <span>© 2026 Performance Feedback</span>
        <span>Talent Toolkit</span>
      </div>
    </footer>
  );
}

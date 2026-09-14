import { STATIC_ASSETS } from '../config/staticAssets';
import {
  performanceManagementDimensions,
  type PerformanceManagementDimensionSlug,
} from './performance-management-content';
import { performanceManagementDimensionPresentation } from './performance-management-presentation';

export type LandingSolutionId = 'strategy-analysis' | 'coaching-goals' | 'feedback-assessment' | 'development-frameworks';

export interface LandingNavGroup {
  id: string;
  label: string;
  href: string;
  description: string;
  descriptionEnglish: string;
  links: readonly { label: string; labelEnglish?: string; href: string }[];
}

export interface LandingHeroContent {
  title: string;
  titleLines: readonly [string, string];
  description: string;
  image: string;
  mobileImage: string;
  imageAlt: string;
}

export interface LandingSolution {
  id: LandingSolutionId;
  label: string;
  href: string;
  title: string;
  description: string;
  descriptionEnglish: string;
  image: string;
  imageAlt: string;
}

export interface LandingResourceModule {
  id: PerformanceManagementDimensionSlug;
  number: string;
  label: string;
  labelEnglish: string;
  href: string;
  title: string;
  titleEnglish: string;
  description: string;
  descriptionEnglish: string;
  image: string;
  imageAlt: string;
  imageAltEnglish: string;
}

export interface HomeExperienceContent {
  navigation: readonly LandingNavGroup[];
  hero: LandingHeroContent;
  resourceModules: readonly LandingResourceModule[];
  solutions: readonly LandingSolution[];
  about: {
    eyebrow: string;
    title: string;
    description: string;
    image: string;
    mobileImage: string;
    imageAlt: string;
    highlights: readonly string[];
  };
}

const LANDING_ASSET = '/assets/landing';
const RESOURCE_ASSET = '/assets/resource';

const RESOURCE_MODULE_ENGLISH_DESCRIPTIONS: Readonly<Record<PerformanceManagementDimensionSlug, string>> = {
  overview: 'Build a shared performance language around value, behavior, the management cycle, and the manager\'s role.',
  'goal-setting': 'Turn business priorities into focused, measurable goals with clear standards, support, and follow-up.',
  'performance-feedback-review': 'Use timely, two-way feedback and observable evidence to clarify contribution, gaps, and next actions.',
  'consequence-management': 'Connect differentiated performance outcomes with appropriate recognition, development, and management actions.',
  'under-performance-management': 'Address sustained gaps early with evidence, clear expectations, close coaching, and compliant follow-through.',
};

const SOLUTION_ENGLISH_DESCRIPTIONS: Readonly<Record<LandingSolutionId, string>> = {
  'strategy-analysis': 'Turn scattered information into a clear, explainable view before making important decisions.',
  'coaching-goals': 'Create a practical rhythm for goals and coaching so reflection, choice, and accountability stay connected.',
  'feedback-assessment': 'Build reliable insight through structured feedback and assessment grounded in observable evidence.',
  'development-frameworks': 'Understand how careers and leadership capabilities grow, then design development paths that fit the transition.',
};

export const homeExperienceContent = {
  navigation: [
    {
      id: 'solutions',
      label: '解决方案',
      href: '/solutions',
      description: '从战略分析、教练目标、反馈评估到发展框架，形成可直接应用的人才工具库。',
      descriptionEnglish: 'A practical talent toolkit spanning strategy, coaching, feedback, assessment, and development.',
      links: [
        { label: 'Strategy & Analysis', href: '/solutions/strategy-analysis' },
        { label: 'Coaching & Goal', href: '/solutions/coaching-goals' },
        { label: 'Feedback & Assessment', href: '/solutions/feedback-assessment' },
        { label: 'Development Framework', href: '/solutions/development-frameworks' },
      ],
    },
    {
      id: 'resources',
      label: '资源中心',
      href: '/resources',
      description: '围绕绩效管理五个维度，快速找到当前阶段需要的方法与行动提示。',
      descriptionEnglish: 'Quickly find the methods and action prompts needed at the current stage across five dimensions of performance management.',
      links: performanceManagementDimensions.map((dimension) => ({
        label: dimension.title,
        labelEnglish: dimension.englishTitle,
        href: dimension.path,
      })),
    },
  ],
  hero: {
    title: '让每一次绩效反馈，成为真实成长的起点',
    titleLines: ['让每一次绩效反馈，', '成为真实成长的起点'],
    description: '55 年专注于领导力：帮企业选得准、育得快、升得对',
    image: STATIC_ASSETS.homeHeroDesktop,
    mobileImage: STATIC_ASSETS.homeHeroMobile,
    imageAlt: '阳光下的山峰、山谷与高山草甸',
  },
  resourceModules: performanceManagementDimensions.map((dimension) => {
    const presentation = performanceManagementDimensionPresentation[dimension.slug];
    return {
      id: dimension.slug,
      number: dimension.number,
      label: dimension.title,
      labelEnglish: dimension.englishTitle,
      href: dimension.path,
      title: dimension.title,
      titleEnglish: dimension.englishTitle,
      description: dimension.summary,
      descriptionEnglish: RESOURCE_MODULE_ENGLISH_DESCRIPTIONS[dimension.slug],
      image: presentation.src,
      imageAlt: presentation.alt,
      imageAltEnglish: presentation.altEnglish,
    };
  }),
  solutions: [
    {
      id: 'strategy-analysis',
      label: 'Strategy & Analysis',
      href: '/solutions/strategy-analysis',
      title: 'Strategy & Analysis',
      description: '在行动之前看清全局，用结构化分析把分散信息转化为清晰、可解释的决策依据。',
      descriptionEnglish: SOLUTION_ENGLISH_DESCRIPTIONS['strategy-analysis'],
      image: `${LANDING_ASSET}/solution-category-strategy-premium-202609.webp`,
      imageAlt: '两名工程师在施工现场共同检查工程进展',
    },
    {
      id: 'coaching-goals',
      label: 'Coaching & Goal',
      href: '/solutions/coaching-goals',
      title: 'Coaching & Goal',
      description: '为目标设定和教练对话建立清晰节奏，让思考、选择与责任真正连接起来。',
      descriptionEnglish: SOLUTION_ENGLISH_DESCRIPTIONS['coaching-goals'],
      image: `${LANDING_ASSET}/solution-category-coaching-premium-202609-v2.webp`,
      imageAlt: '两位专业人士在明亮办公室进行一对一辅导交流',
    },
    {
      id: 'feedback-assessment',
      label: 'Feedback & Assessment',
      href: '/solutions/feedback-assessment',
      title: 'Feedback & Assessment',
      description: '用结构化反馈和评估建立可靠认知，让发展讨论从印象走向可验证的证据。',
      descriptionEnglish: SOLUTION_ENGLISH_DESCRIPTIONS['feedback-assessment'],
      image: `${LANDING_ASSET}/solution-category-feedback-premium-202609.webp`,
      imageAlt: '玻璃倒影中的专业人员在开放办公空间协作',
    },
    {
      id: 'development-frameworks',
      label: 'Development Framework',
      href: '/solutions/development-frameworks',
      title: 'Development Framework',
      description: '理解职业与管理能力如何成长，识别角色转换中的断点，并设计更匹配的发展路径。',
      descriptionEnglish: SOLUTION_ENGLISH_DESCRIPTIONS['development-frameworks'],
      image: `${LANDING_ASSET}/solution-category-development-premium-202609-v2.webp`,
      imageAlt: '一组专业人士在发展研讨现场专注聆听',
    },
  ],
  about: {
    eyebrow: '关于 Performance Feedback',
    title: '全球领先的人才评鉴与领导力发展专家',
    description: '关于人才，只有真正理解人的成长规律，才能帮助组织在变化中持续前行。',
    image: `${RESOURCE_ASSET}/contact-desktop.png?v=20260805-080501`,
    mobileImage: `${RESOURCE_ASSET}/contact-mobile.jpg?v=20260805-080501`,
    imageAlt: '人才与领导力发展品牌视觉',
    highlights: ['科学的人才评鉴', '体系化领导力发展', '全球视野与本土实践'],
  },
} satisfies HomeExperienceContent;

import type { PerformanceManagementDimensionSlug } from './performance-management-content';

export interface PerformanceManagementDimensionPresentation {
  src: string;
  alt: string;
  altEnglish: string;
  width: number;
  height: number;
  heroPosition: string;
}

export const performanceManagementDimensionPresentation: Readonly<
  Record<PerformanceManagementDimensionSlug, PerformanceManagementDimensionPresentation>
> = Object.freeze({
  overview: {
    src: '/assets/landing/solution-strategy.webp',
    alt: '团队围绕绩效议题进行讨论',
    altEnglish: 'A team discussing performance topics',
    width: 1800,
    height: 1000,
    heroPosition: 'center center',
  },
  'goal-setting': {
    src: '/assets/landing/solution-coaching.webp',
    alt: '管理者与员工进行一对一沟通',
    altEnglish: 'A manager and employee in a one-to-one conversation',
    width: 1800,
    height: 1000,
    heroPosition: 'center center',
  },
  'performance-feedback-review': {
    src: '/assets/landing/solution-feedback.webp',
    alt: '团队在屏幕前复盘工作信息',
    altEnglish: 'A team reviewing work information on a screen',
    width: 1800,
    height: 1000,
    heroPosition: 'center center',
  },
  'consequence-management': {
    src: '/assets/landing/solution-development.jpg',
    alt: '团队成员共同确认行动方向',
    altEnglish: 'Team members aligning on a course of action',
    width: 1594,
    height: 655,
    heroPosition: 'center center',
  },
  'under-performance-management': {
    src: '/assets/landing/solution-hr-enablement.jpg',
    alt: '管理者使用数字化人力资源工具',
    altEnglish: 'A manager using digital HR tools',
    width: 1600,
    height: 658,
    heroPosition: 'center center',
  },
});

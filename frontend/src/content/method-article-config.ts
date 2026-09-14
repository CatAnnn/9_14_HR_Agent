export interface MethodSectionConfig {
  id: string;
  label: string;
}

export interface MethodArticleConfig {
  readingTime: string;
  meta?: readonly string[];
  sections: readonly MethodSectionConfig[];
}

const genericSections = [
  { id: 'method-overview', label: '方法概述' },
  { id: 'method-framework', label: '核心框架' },
  { id: 'method-process', label: '应用步骤' },
  { id: 'method-application', label: '使用场景' },
  { id: 'method-pitfalls', label: '常见误区' },
  { id: 'method-manager', label: '经理应用' },
] as const;

export const methodArticleConfigs = {
  'swot-analysis': {
    readingTime: '约 10 分钟阅读',
    sections: [
      { id: 'method-overview', label: '概述与价值' },
      { id: 'method-framework', label: 'SWOT 四象限' },
      { id: 'method-example', label: '应用示例' },
      { id: 'method-process', label: '分析步骤' },
      { id: 'method-benefits', label: '主要收益' },
      { id: 'method-application', label: '使用场景' },
      { id: 'method-pitfalls', label: '常见误区' },
      { id: 'method-manager', label: '经理应用' },
    ],
  },
  'vrio-analysis': {
    readingTime: '约 10 分钟阅读',
    sections: [
      { id: 'method-overview', label: '概述与价值' },
      { id: 'method-framework', label: '四项检验' },
      { id: 'method-example', label: '分析示例' },
      { id: 'method-process', label: '应用步骤' },
      { id: 'method-outcomes', label: '竞争结果' },
      { id: 'method-application', label: '使用场景' },
      { id: 'method-pitfalls', label: '常见误区' },
      { id: 'method-manager', label: '经理应用' },
    ],
  },
  'mckinsey-7s': {
    readingTime: '约 12 分钟阅读',
    meta: ['Organizational alignment guide'],
    sections: [
      { id: 'method-overview', label: '起源与核心观点' },
      { id: 'method-framework', label: '七个组织要素' },
      { id: 'method-diagnostics', label: '诊断问题' },
      { id: 'method-example', label: '并购案例' },
      { id: 'method-process', label: '变革方法' },
      { id: 'method-comparison', label: '模型对照' },
      { id: 'method-application', label: '使用场景' },
      { id: 'method-pitfalls', label: '常见误区' },
      { id: 'method-presentation', label: '呈现方式' },
      { id: 'method-manager', label: '经理应用' },
    ],
  },
  'skills-gap-analysis': {
    readingTime: '约 14 分钟阅读',
    meta: ['组织 · 团队 · 个人'],
    sections: [
      { id: 'skills-gap-overview', label: '定义与业务价值' },
      { id: 'skills-gap-types', label: '技能差距类型' },
      { id: 'skills-gap-levels', label: '三层分析' },
      { id: 'skills-gap-process', label: '完整分析步骤' },
      { id: 'skills-gap-matrix', label: '现状与目标矩阵' },
      { id: 'skills-gap-evidence', label: '数据来源' },
      { id: 'skills-gap-priority', label: '优先级判断' },
      { id: 'skills-gap-actions', label: '行动方案' },
      { id: 'skills-gap-example', label: '应用示例' },
      { id: 'skills-gap-pitfalls', label: '常见误区' },
      { id: 'skills-gap-checklist', label: '检查清单' },
    ],
  },
  'grow-model': {
    readingTime: '约 14 分钟阅读',
    meta: ['Coaching conversation guide'],
    sections: [
      { id: 'grow-overview', label: '起源与用途' },
      { id: 'grow-framework', label: '四阶段路径' },
      { id: 'grow-questions', label: '高质量问题库' },
      { id: 'grow-dialogue', label: '教练对话示例' },
      { id: 'grow-session', label: '一次会话流程' },
      { id: 'grow-practice', label: '场景与自我教练' },
      { id: 'grow-guardrails', label: '限制与常见错误' },
      { id: 'grow-action', label: '行动承诺与复盘' },
    ],
  },
  'smart-goals': {
    readingTime: '约 14 分钟阅读',
    meta: ['实践版 · 2026'],
    sections: [
      { id: 'smart-definition', label: '定义与价值' },
      { id: 'smart-criteria', label: '五项标准' },
      { id: 'smart-example', label: '完整示例' },
      { id: 'smart-writing', label: '写作步骤与模板' },
      { id: 'smart-work', label: '连接日常工作' },
      { id: 'smart-tracking', label: '跟踪与复盘' },
      { id: 'smart-okr', label: 'SMART 与 OKR' },
      { id: 'smart-tradeoffs', label: '优点与限制' },
      { id: 'smart-mistakes', label: '常见错误' },
      { id: 'smart-checklist', label: '发布前检查' },
    ],
  },
  'career-coaching-tools': {
    readingTime: '约 8 分钟阅读',
    sections: genericSections,
  },
  '360-degree-feedback': {
    readingTime: '约 12 分钟阅读',
    sections: [
      { id: 'f360-overview', label: '方法价值' },
      { id: 'f360-coverage', label: '视角覆盖' },
      { id: 'f360-blueprint', label: '题项蓝图' },
      { id: 'f360-process', label: '六步实施' },
      { id: 'f360-compare', label: '自他差异' },
      { id: 'f360-debrief', label: '反馈会谈' },
      { id: 'f360-action', label: '行动与跟进' },
      { id: 'f360-safeguards', label: '治理护栏' },
    ],
  },
  'leadership-self-assessment': {
    readingTime: '约 12 分钟阅读',
    sections: [
      { id: 'lsa-foundation', label: '评估原则' },
      { id: 'lsa-competencies', label: '能力模型' },
      { id: 'lsa-scale', label: '行为量规' },
      { id: 'lsa-evidence', label: '证据账本' },
      { id: 'lsa-calibration', label: '外部校准' },
      { id: 'lsa-profile', label: '示例画像' },
      { id: 'lsa-action', label: '行动计划' },
      { id: 'lsa-rollout', label: '组织应用' },
    ],
  },
  'belbin-team-roles': {
    readingTime: '约 12 分钟阅读',
    sections: [
      { id: 'belbin-foundation', label: '先理解边界' },
      { id: 'belbin-role-explorer', label: '九类贡献' },
      { id: 'belbin-coverage', label: '覆盖规划' },
      { id: 'belbin-project', label: '项目阶段' },
      { id: 'belbin-collaboration', label: '协作组合' },
      { id: 'belbin-workshop', label: '团队工作坊' },
      { id: 'belbin-guardrails', label: '使用边界' },
    ],
  },
  'soft-skills-assessment': {
    readingTime: '约 10 分钟阅读',
    sections: [
      { id: 'ssa-overview', label: '方法价值' },
      { id: 'ssa-model', label: '评估模型' },
      { id: 'ssa-profiles', label: '能力画像' },
      { id: 'ssa-evidence', label: '证据来源' },
      { id: 'ssa-methods', label: '方法选择' },
      { id: 'ssa-scenario', label: '现实情境' },
      { id: 'ssa-scoring', label: '评分方式' },
      { id: 'ssa-development', label: '发展实验' },
      { id: 'ssa-fairness', label: '公平边界' },
    ],
  },
  'workplace-skills-assessment': {
    readingTime: '约 14 分钟阅读',
    sections: [
      { id: 'wsa-overview', label: '方法价值' },
      { id: 'wsa-operating-model', label: '六段式主线' },
      { id: 'wsa-role-canvas', label: '岗位画布' },
      { id: 'wsa-architecture', label: '技能架构' },
      { id: 'wsa-rubrics', label: '等级标尺' },
      { id: 'wsa-methods', label: '方法矩阵' },
      { id: 'wsa-work-sample', label: '真实工作样本' },
      { id: 'wsa-portfolio', label: '证据组合' },
      { id: 'wsa-gap', label: '差距优先级' },
      { id: 'wsa-development', label: '发展与治理' },
      { id: 'wsa-sources', label: '来源记录' },
    ],
  },
  'peter-principle': {
    readingTime: '约 8 分钟阅读',
    sections: genericSections,
  },
  'management-models': {
    readingTime: '约 8 分钟阅读',
    sections: genericSections,
  },
  'professional-development': {
    readingTime: '约 8 分钟阅读',
    sections: genericSections,
  },
} as const satisfies Record<string, MethodArticleConfig>;

export function getMethodArticleConfig(toolId: string): MethodArticleConfig {
  return methodArticleConfigs[toolId as keyof typeof methodArticleConfigs] ?? {
    readingTime: '约 8 分钟阅读',
    sections: genericSections,
  };
}

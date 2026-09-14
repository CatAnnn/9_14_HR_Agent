export const PERFORMANCE_MANAGEMENT_BASE_PATH = '/resources/performance-management' as const;

export const PERFORMANCE_MANAGEMENT_DIMENSION_SLUGS = [
  'overview',
  'goal-setting',
  'performance-feedback-review',
  'consequence-management',
  'under-performance-management',
] as const;

export const PERFORMANCE_MANAGEMENT_SOURCE_CHAPTERS = [1, 2, 3, 4, 5] as const;
export const PERFORMANCE_MANAGEMENT_OVERVIEW_SOURCE_CHAPTER = 1 as const;

export type PerformanceManagementDimensionSlug =
  (typeof PERFORMANCE_MANAGEMENT_DIMENSION_SLUGS)[number];

export type PerformanceManagementSourceChapter =
  (typeof PERFORMANCE_MANAGEMENT_SOURCE_CHAPTERS)[number];

export type PerformanceManagementContentStatus = 'ready' | 'draft' | 'policy-review';

interface PerformanceManagementSectionBase {
  id: string;
  title: string;
  eyebrow?: string;
  description?: string;
  status?: PerformanceManagementContentStatus;
  draftNote?: string;
}

export interface PerformanceManagementCardItem {
  id: string;
  title: string;
  englishTitle?: string;
  description: string;
  bullets?: readonly string[];
  note?: string;
}

export interface PerformanceManagementCardsSection
  extends PerformanceManagementSectionBase {
  kind: 'cards';
  layout?: 'two-column' | 'three-column' | 'four-column';
  items: readonly PerformanceManagementCardItem[];
}

export interface PerformanceManagementComparisonColumn {
  id: string;
  title: string;
  englishTitle?: string;
  description?: string;
  items: readonly string[];
}

export interface PerformanceManagementComparisonSection
  extends PerformanceManagementSectionBase {
  kind: 'comparison';
  bridgeLabel?: string;
  columns: readonly [
    PerformanceManagementComparisonColumn,
    PerformanceManagementComparisonColumn,
  ];
}

export interface PerformanceManagementStepItem {
  id: string;
  order: number;
  title: string;
  englishTitle?: string;
  task: string;
  prompt?: string;
  note?: string;
}

export interface PerformanceManagementStepsSection
  extends PerformanceManagementSectionBase {
  kind: 'steps';
  steps: readonly PerformanceManagementStepItem[];
}

export interface PerformanceManagementTableColumn {
  key: string;
  label: string;
}

export interface PerformanceManagementTableRow {
  id: string;
  cells: readonly string[];
}

export interface PerformanceManagementTableSection
  extends PerformanceManagementSectionBase {
  kind: 'table';
  caption?: string;
  columns: readonly PerformanceManagementTableColumn[];
  rows: readonly PerformanceManagementTableRow[];
}

export interface PerformanceManagementFlowItem {
  id: string;
  title: string;
  englishTitle?: string;
  description: string;
  note?: string;
}

export interface PerformanceManagementFlowSection
  extends PerformanceManagementSectionBase {
  kind: 'flow';
  orientation?: 'horizontal' | 'vertical';
  items: readonly PerformanceManagementFlowItem[];
}

export interface PerformanceManagementMatrixAxis {
  label: string;
  lowLabel: string;
  highLabel: string;
}

export interface PerformanceManagementMatrixCell {
  id: string;
  x: 1 | 2 | 3;
  y: 1 | 2 | 3;
  title: string;
  description?: string;
  status?: PerformanceManagementContentStatus;
  draftNote?: string;
}

export interface PerformanceManagementMatrixSection
  extends PerformanceManagementSectionBase {
  kind: 'matrix';
  xAxis: PerformanceManagementMatrixAxis;
  yAxis: PerformanceManagementMatrixAxis;
  cells: readonly PerformanceManagementMatrixCell[];
}

export interface PerformanceManagementRatingItem {
  level: 1 | 2 | 3 | 4 | 5;
  title: string;
  audience: string;
  focus: string;
  status?: PerformanceManagementContentStatus;
  draftNote?: string;
}

export interface PerformanceManagementRatingSection
  extends PerformanceManagementSectionBase {
  kind: 'rating';
  items: readonly PerformanceManagementRatingItem[];
}

export interface PerformanceManagementCalloutSection
  extends PerformanceManagementSectionBase {
  kind: 'callout';
  tone: 'info' | 'success' | 'warning' | 'policy';
  body: string;
  bullets?: readonly string[];
}

export type PerformanceManagementSection =
  | PerformanceManagementCardsSection
  | PerformanceManagementComparisonSection
  | PerformanceManagementStepsSection
  | PerformanceManagementTableSection
  | PerformanceManagementFlowSection
  | PerformanceManagementMatrixSection
  | PerformanceManagementRatingSection
  | PerformanceManagementCalloutSection;

export interface PerformanceManagementDimension {
  order: 1 | 2 | 3 | 4 | 5;
  number: '01' | '02' | '03' | '04' | '05';
  sourceChapter: PerformanceManagementSourceChapter;
  slug: PerformanceManagementDimensionSlug;
  path: `${typeof PERFORMANCE_MANAGEMENT_BASE_PATH}/${PerformanceManagementDimensionSlug}`;
  title: string;
  englishTitle: string;
  tagline: string;
  summary: string;
  objective: string;
  keywords: readonly string[];
  status: PerformanceManagementContentStatus;
  draftNote?: string;
  sections: readonly PerformanceManagementSection[];
}

export interface PerformanceManagementOverviewContent extends PerformanceManagementDimension {
  order: 1;
  number: '01';
  sourceChapter: typeof PERFORMANCE_MANAGEMENT_OVERVIEW_SOURCE_CHAPTER;
  slug: 'overview';
  path: `${typeof PERFORMANCE_MANAGEMENT_BASE_PATH}/overview`;
  title: string;
  englishTitle: 'Overview';
  tagline: string;
  summary: string;
  objective: string;
  keywords: readonly string[];
  status: PerformanceManagementContentStatus;
  draftNote?: string;
  sections: readonly PerformanceManagementSection[];
}

export const performanceManagementOverviewContent: PerformanceManagementOverviewContent = {
    order: 1,
    number: '01',
    sourceChapter: PERFORMANCE_MANAGEMENT_OVERVIEW_SOURCE_CHAPTER,
    slug: 'overview',
    path: `${PERFORMANCE_MANAGEMENT_BASE_PATH}/overview`,
    title: '概述篇：认知重塑',
    englishTitle: 'Overview',
    tagline: '从“评估者”转变为“赋能者”',
    summary: '理解高绩效文化、绩效管理本质、管理闭环，以及经理在不同绩效场景中的四种角色。',
    objective: '建立共同语言：既关注交付了什么价值，也关注以怎样的能力与行为达成。',
    keywords: ['高绩效文化', 'Evaluator', 'Enabler', 'WHAT', 'HOW', '绩效闭环', '经理角色'],
    status: 'draft',
    draftNote: '原稿中的高绩效文化说明仍有编辑标记；本页只呈现已能从正文与图表确认的内容。',
    sections: [
      {
        id: 'overview-high-performance-culture',
        kind: 'cards',
        eyebrow: 'RBCN High Performance Culture',
        title: '高绩效文化意味着什么',
        description: '持续交付卓越业务结果、提升竞争力，并以可持续的方式不断进步，对客户、员工与社会产生积极影响。',
        status: 'ready',
        layout: 'three-column',
        items: [
          {
            id: 'overview-customer-success',
            title: '成就客户',
            englishTitle: 'Enabling Customer Success',
            description: '洞察需求，超越预期，不断创造新价值。',
          },
          {
            id: 'overview-grow-through-innovation',
            title: '创新破局',
            englishTitle: 'Grow through Innovation',
            description: '打破常规，迎难而上，持续探索最优解。',
          },
          {
            id: 'overview-win-as-one',
            title: '协力共赢',
            englishTitle: 'Win as One',
            description: '凝聚共识，相互借力，果断把握新机遇。',
          },
        ],
      },
      {
        id: 'overview-enabler-shift',
        kind: 'comparison',
        eyebrow: 'Role Shift',
        title: '经理角色转变',
        description: '绩效管理不只是节点上的判断，更是通过持续对话帮助团队创造价值。',
        status: 'draft',
        draftNote: '原稿仅提出角色转变方向，未给出完整的“评估者”行为清单。',
        bridgeLabel: '从单一评判走向持续赋能',
        columns: [
          {
            id: 'overview-evaluator',
            title: '评估者',
            englishTitle: 'Evaluator',
            description: '原稿提出需要走出的单一“考官”定位。',
            items: ['不把绩效管理等同于一次性的结果评判。'],
          },
          {
            id: 'overview-enabler',
            title: '赋能者',
            englishTitle: 'Enabler',
            description: '把管理重点放在价值创造、优势激发与障碍清除上。',
            items: [
              '通过日常对话激发员工优势。',
              '为员工和团队扫清障碍。',
              '确保团队贡献与公司战略目标保持一致。',
            ],
          },
        ],
      },
      {
        id: 'overview-performance-definition',
        kind: 'cards',
        eyebrow: 'Performance = Deliverables + Competence + Behavior',
        title: '绩效管理的 WHAT 与 HOW',
        description: '绩效反映一个人在当前角色、职责和期望下的交付情况；绩效管理则是主管与员工共同参与的持续沟通过程。',
        status: 'ready',
        layout: 'two-column',
        items: [
          {
            id: 'overview-what',
            title: '核心业务贡献',
            englishTitle: 'WHAT · Core Business Contribution',
            description: '在项目、财务指标或流程优化上交付直接、可衡量、有价值的成果。',
            bullets: ['看目标达成，更看是否解决业务痛点。', '从“完成任务”进一步追问“创造了什么价值”。'],
          },
          {
            id: 'overview-how',
            title: '能力与行为展现',
            englishTitle: 'HOW · Competence & Behavior',
            description: '关注达成成果过程中展现的能力和行为，例如协作、创新与持续改进。',
            bullets: ['观察行为是否一贯、可重复。', '结合关键事件与跨部门反馈形成事实依据。'],
          },
        ],
      },
      {
        id: 'overview-management-cycle',
        kind: 'flow',
        eyebrow: 'Performance Management Cycle',
        title: '绩效管理闭环',
        description: '从目标设定到日常反馈、综合评估、结果管理与薪酬衔接，形成持续循环。',
        status: 'ready',
        orientation: 'horizontal',
        items: [
          {
            id: 'overview-cycle-goal-setting',
            title: '目标设定',
            englishTitle: 'Goal Setting',
            description: '基于公司目标和价值观，定义个人目标及所需能力。',
          },
          {
            id: 'overview-cycle-feedback',
            title: '绩效反馈',
            englishTitle: 'Performance Feedback',
            description: '定期检查目标进展、能力与行为表现。',
          },
          {
            id: 'overview-cycle-review',
            title: '绩效评估',
            englishTitle: 'Performance Review',
            description: '围绕 WHAT 与 HOW 综合评估个人绩效。',
          },
          {
            id: 'overview-cycle-consequence',
            title: '结果管理',
            englishTitle: 'Consequence Management',
            description: '针对不同绩效表现实施合适的发展或改进措施。',
          },
          {
            id: 'overview-cycle-compensation',
            title: '薪酬衔接',
            englishTitle: 'Compensation',
            description: '绩效结果与薪酬决策按正式政策衔接。',
            note: '具体规则以公司正式政策为准。',
          },
        ],
      },
      {
        id: 'overview-manager-roles',
        kind: 'cards',
        eyebrow: 'Manager’s Role',
        title: '经理的四种角色',
        description: '根据管理场景灵活切换视角，既代表组织，也提供专业、团队与个人层面的支持。',
        status: 'ready',
        layout: 'two-column',
        items: [
          {
            id: 'overview-company-representative',
            title: '公司代表',
            englishTitle: 'Company Representative',
            description: '将愿景、使命与战略清晰传导给员工，并以组织整体利益和业务背景为判断基准。',
            bullets: ['聚焦结果与关键任务。', '分析业务背景与成本效益。', '在校准和艰难对话中维护组织长远目标。'],
          },
          {
            id: 'overview-content-expert',
            title: '内容专家',
            englishTitle: 'Content Expert',
            description: '围绕方案可行性、过程、计划、资源、能力、质量与效率开展专业对话。',
            bullets: ['辅导解决问题。', '推动优化与持续改进。', '共创可行路径并分享成功经验。'],
          },
          {
            id: 'overview-team-coach',
            title: '团队教练',
            englishTitle: 'Team Coach',
            description: '通过认可、激励与个性化辅导，发展个人优势并促进团队内外协作。',
            bullets: ['表达认可与赞赏。', '根据个人特质与需要提供发展支持。', '帮助团队与多方利益相关者协作。'],
          },
          {
            id: 'overview-personally-involved',
            title: '共情伙伴',
            englishTitle: 'Personally Involved Individual',
            description: '以真诚、平等的方式表达感受和态度，并理解员工的情绪与主观视角。',
            bullets: ['建立安全、可信任的沟通氛围。', '主动倾听并展现真实自我。'],
          },
        ],
      },
    ],
};

const performanceManagementDimensionData = [
  performanceManagementOverviewContent,
  {
    order: 2,
    number: '02',
    sourceChapter: 2,
    slug: 'goal-setting',
    path: `${PERFORMANCE_MANAGEMENT_BASE_PATH}/goal-setting`,
    title: '目标设定篇：化战略为行动',
    englishTitle: 'Goal Setting',
    tagline: '找到有价值的事，再把它写成可执行的目标',
    summary: '从业务和客户痛点出发，通过 VCP 漏斗聚焦 3–5 个重点，并用 SMART 与 WHAT + HOW 形成目标共识。',
    objective: '帮助员工把战略翻译成个人的高价值贡献，并明确评价标准、支持方式与跟进机制。',
    keywords: ['目标设定', 'VCP', 'Look Up', 'Look Across', 'Focus Down', 'SMART', 'WHAT + HOW', '五步谈话'],
    status: 'ready',
    sections: [
      {
        id: 'goal-setting-why-goals-matter',
        kind: 'cards',
        eyebrow: 'Why Goals Matter',
        title: '目标为三方创造共同方向',
        description: '目标是在特定时间内希望实现的、可衡量的具体结果，它描述未来期望达到的状态。',
        status: 'ready',
        layout: 'three-column',
        items: [
          {
            id: 'goal-setting-company-value',
            title: '对公司',
            description: '承接战略并优化资源配置，让投入集中到最重要的业务结果。',
          },
          {
            id: 'goal-setting-manager-value',
            title: '对管理者',
            description: '提升管理效率、凝聚团队，并在交付过程中培养人才。',
          },
          {
            id: 'goal-setting-employee-value',
            title: '对员工',
            description: '提供方向感和动力，助力职业发展并增强工作幸福感。',
          },
        ],
      },
      {
        id: 'goal-setting-vcp-funnel',
        kind: 'flow',
        eyebrow: 'VCP Funnel',
        title: '从战略到个人重点',
        description: '先升维理解业务痛点，再降维拆解价值贡献，拒绝目标碎片化。',
        status: 'ready',
        orientation: 'horizontal',
        items: [
          {
            id: 'goal-setting-look-up',
            title: '看战略',
            englishTitle: 'Look Up',
            description: '理解部门或事业部 VCP 的核心财务与非财务指标，例如技术创新、效率提升与 AI 应用。',
          },
          {
            id: 'goal-setting-look-across',
            title: '看协同',
            englishTitle: 'Look Across',
            description: '明确跨团队协作中的价值交付点，理解自己的工作如何影响共同结果。',
          },
          {
            id: 'goal-setting-focus-down',
            title: '定重点',
            englishTitle: 'Focus Down',
            description: '每位员工聚焦 3–5 个对 VCP 贡献最大的高价值重点。',
          },
        ],
      },
      {
        id: 'goal-setting-task-to-value',
        kind: 'comparison',
        eyebrow: 'Task List → Value Contribution',
        title: '从任务清单转向价值贡献',
        description: '好的目标没有脱离业务的标准答案，关键是先找准要解决的痛点。',
        status: 'draft',
        draftNote: '原稿预留了销售、研发、PRM 与客户支持案例，但尚未提供可发布的完整示例。',
        bridgeLabel: '追问任务背后的业务价值',
        columns: [
          {
            id: 'goal-setting-task-list',
            title: '任务清单',
            englishTitle: 'Task List',
            description: '有 KPI，却可能各自分散、彼此缺乏联系。',
            items: ['描述“要做哪些事情”。', '容易停留在活动或完成动作层面。'],
          },
          {
            id: 'goal-setting-value-contribution',
            title: '价值贡献',
            englishTitle: 'Value Contribution',
            description: '把任务转化为能够解决业务或客户痛点的价值产出。',
            items: ['说明“要改变什么结果”。', '与 VCP 和跨团队价值交付保持连接。'],
          },
        ],
      },
      {
        id: 'goal-setting-smart-checklist',
        kind: 'cards',
        eyebrow: 'SMART Checklist',
        title: '把价值重点写成 SMART 目标',
        description: '用五个问题检查目标是否清晰、可判断且能够落地。',
        status: 'ready',
        layout: 'three-column',
        items: [
          {
            id: 'goal-setting-smart-specific',
            title: 'S · 具体',
            englishTitle: 'Specific',
            description: '目标是否明确描述了要交付的结果，而不只是宽泛方向？',
          },
          {
            id: 'goal-setting-smart-measurable',
            title: 'M · 可衡量',
            englishTitle: 'Measurable',
            description: '是否能用数据、事实或清晰标准判断目标是否达成？',
          },
          {
            id: 'goal-setting-smart-achievable',
            title: 'A · 可实现',
            englishTitle: 'Achievable',
            description: '结合资源、能力与挑战后，目标是否具有可行的达成路径？',
          },
          {
            id: 'goal-setting-smart-relevant',
            title: 'R · 相关',
            englishTitle: 'Relevant',
            description: '目标是否回应业务痛点，并与团队 VCP 重点直接相关？',
          },
          {
            id: 'goal-setting-smart-time-bound',
            title: 'T · 有时限',
            englishTitle: 'Time-bound',
            description: '是否明确完成时间及必要的阶段检查点？',
          },
        ],
      },
      {
        id: 'goal-setting-what-how-formula',
        kind: 'cards',
        eyebrow: 'WHAT + HOW',
        title: '目标既写结果，也写达成方式',
        description: '每个重点目标都应同时定义业务产出，以及为达成目标所需的能力成长或行为展现。',
        status: 'ready',
        layout: 'two-column',
        items: [
          {
            id: 'goal-setting-what-output',
            title: 'WHAT · 业务产出',
            description: '用 SMART 原则定义具体、可衡量、有价值的交付结果。',
          },
          {
            id: 'goal-setting-how-growth',
            title: 'HOW · 能力与行为',
            description: '明确达成目标过程中需要发展的能力与应展现的核心行为。',
          },
        ],
      },
      {
        id: 'goal-setting-conversation',
        kind: 'steps',
        eyebrow: 'Five-step Conversation',
        title: '绩效目标五步谈话法',
        description: '由员工参与思考与共创，让目标、评价标准、支持方式和跟进机制成为双向承诺。',
        status: 'ready',
        steps: [
          {
            id: 'goal-setting-step-open',
            order: 1,
            title: '开场',
            englishTitle: 'Open',
            task: '建立轻松、平等的谈话基调，重申谈话是为了支持员工发展。',
            prompt: '“今天我们不只谈任务，也谈谈你今年最想在哪些方面为团队做出有价值的贡献。”',
          },
          {
            id: 'goal-setting-step-clarify',
            order: 2,
            title: '澄清',
            englishTitle: 'Clarify',
            task: '结合部门 VCP，探讨员工想法与业务重点是否对齐，并明确具体期望。',
            prompt: '“结合团队今年的 VCP 重点，你负责的项目最核心的交付价值是什么？”',
          },
          {
            id: 'goal-setting-step-develop',
            order: 3,
            title: '推演',
            englishTitle: 'Develop',
            task: '启发员工思考如何达成，评估潜在挑战及所需资源。',
            prompt: '“要实现这个目标，最大的挑战在哪？我或团队可以提供什么支持？”',
          },
          {
            id: 'goal-setting-step-agree',
            order: 4,
            title: '共识',
            englishTitle: 'Agree',
            task: '对目标内容、评价标准和主管支持达成双向承诺。',
            prompt: '“我们把这几个重点作为你的核心贡献目标，我会按约定提供支持。”',
          },
          {
            id: 'goal-setting-step-close',
            order: 5,
            title: '收尾',
            englishTitle: 'Close',
            task: '明确后续跟进机制，并将共识记录在 People Dialogs 中。',
            prompt: '“请把今天的共识记录下来，我们后续在 1:1 中持续跟进。”',
            note: '业务重点变化时，可在 People Dialogs 中敏捷更新目标。',
          },
        ],
      },
    ],
  },
  {
    order: 3,
    number: '03',
    sourceChapter: 3,
    slug: 'performance-feedback-review',
    path: `${PERFORMANCE_MANAGEMENT_BASE_PATH}/performance-feedback-review`,
    title: '绩效反馈与评估',
    englishTitle: 'Performance Feedback & Review',
    tagline: '用事实看见价值，用反馈推动下一步行动',
    summary: '建立及时、建设性、双向的反馈习惯，以 WHAT/HOW 证据和 SBI 方法完成客观评估与结果沟通。',
    objective: '让员工清楚理解贡献、差距和发展方向，同时保持评价公平、具体且可行动。',
    keywords: ['绩效反馈', '绩效评估', 'Value Delivery', 'Timely', 'Constructive', 'Two-way', 'SBI', 'WHAT', 'HOW', '评级'],
    status: 'draft',
    draftNote: '五级评级定义仍属于草稿框架；页面只用于沟通准备，不替代正式校准与政策。',
    sections: [
      {
        id: 'performance-feedback-review-from-to',
        kind: 'table',
        eyebrow: 'From → To',
        title: '反馈焦点的四个转变',
        description: '从活动、年底总账和主观标签，转向价值、及时反馈、事实依据与共同解决。',
        status: 'ready',
        caption: '绩效反馈的 From–To 对照',
        columns: [
          { key: 'perspective', label: '视角' },
          { key: 'from', label: 'From' },
          { key: 'to', label: 'To' },
        ],
        rows: [
          {
            id: 'performance-feedback-review-value',
            cells: ['价值创造', '完成了哪些任务；关注加班、开会等活动指标', '交付了什么价值；对业务或客户带来了什么增值'],
          },
          {
            id: 'performance-feedback-review-timeliness',
            cells: ['及时反馈', '问题累积到年底一次性反馈', '事情发生时立即认可或纠偏'],
          },
          {
            id: 'performance-feedback-review-evidence',
            cells: ['聚焦业务', '用“工作不主动”“态度差”等主观标签', '基于具体情境、行为与可量化影响'],
          },
          {
            id: 'performance-feedback-review-support',
            cells: ['有效支持', '只指出哪里做错了', '共同探讨解决方案与未来改进行动'],
          },
        ],
      },
      {
        id: 'performance-feedback-review-principles',
        kind: 'cards',
        eyebrow: 'Feedback Principles',
        title: '高质量反馈的三个原则',
        description: '以战略为导向、以数据和事实为依据、以公平为原则、以激励和行动为目标。',
        status: 'ready',
        layout: 'three-column',
        items: [
          {
            id: 'performance-feedback-review-timely',
            title: '及时性',
            englishTitle: 'Timely',
            description: '拒绝“秋后算账”，在事情发生时就给予认可或辅导。',
          },
          {
            id: 'performance-feedback-review-constructive',
            title: '建设性',
            englishTitle: 'Constructive',
            description: '不指责、不贴标签，聚焦具体事实与解决方案。',
          },
          {
            id: 'performance-feedback-review-two-way',
            title: '双向性',
            englishTitle: 'Two-way',
            description: '鼓励员工提供向上反馈，创造能够坦诚交流的心理安全感。',
          },
        ],
      },
      {
        id: 'performance-feedback-review-evidence-matrix',
        kind: 'table',
        eyebrow: 'Evidence Matrix',
        title: 'WHAT / HOW 证据矩阵',
        description: '把价值结果与达成方式放在同一张评估视图中，减少单一指标或印象判断。',
        status: 'ready',
        caption: '绩效综合评估的两个维度',
        columns: [
          { key: 'dimension', label: '维度' },
          { key: 'what', label: 'WHAT · 核心业务贡献' },
          { key: 'how', label: 'HOW · 能力与行为展现' },
        ],
        rows: [
          { id: 'performance-feedback-review-object', cells: ['评估对象', '交付的结果与价值影响', '达成结果过程中的行为方式'] },
          { id: 'performance-feedback-review-source', cells: ['证据来源', '目标数据、业务指标、客户反馈、项目复盘', '关键事件与跨部门反馈'] },
          { id: 'performance-feedback-review-tool', cells: ['评估工具', '实际交付与承诺目标对比', 'SBI：情境—行为—影响'] },
          { id: 'performance-feedback-review-time', cells: ['时间视角', '回顾全年交付总和', '观察行为是否一贯、可重复'] },
          { id: 'performance-feedback-review-question', cells: ['核心提问', '“你解决了什么业务痛点？”', '“你在过程中如何与他人协作？”'] },
        ],
      },
      {
        id: 'performance-feedback-review-rating',
        kind: 'rating',
        eyebrow: 'Performance Rating',
        title: '五级结果沟通视角',
        description: '草稿将绩效结果分为五级；这里仅呈现对应沟通关注点，不发布评级比例、奖金系数或强制措施。',
        status: 'policy-review',
        draftNote: '评级定义、适用范围、分布与后果须以正式政策及校准结果为准。',
        items: [
          { level: 1, title: 'Outstanding', audience: '高绩效者', focus: '认可与保留，探讨长期职业抱负和下一项有挑战性的目标。', status: 'policy-review' },
          { level: 2, title: 'Exceed', audience: '高绩效者', focus: '认可超出期望的贡献，讨论如何延展影响并继续成长。', status: 'policy-review' },
          { level: 3, title: 'Achieved', audience: '坚实贡献者', focus: '肯定稳定交付，并对齐下一阶段 VCP 的成长与突破重点。', status: 'policy-review' },
          { level: 4, title: 'Partially Achieved', audience: '有待改进者', focus: '基于事实诊断能力或交付差距，提供辅导并紧密跟进。', status: 'policy-review' },
          { level: 5, title: 'Not Achieved', audience: '未达成者', focus: '开展坦诚且合规的艰难对话，并尽早与 HRBP 对齐后续流程。', status: 'policy-review', draftNote: 'PIP 与劳动关系等动作不得仅依据本草稿自动触发。' },
        ],
      },
      {
        id: 'performance-feedback-review-sbi',
        kind: 'cards',
        eyebrow: 'SBI',
        title: '用 SBI 说清事实与影响',
        description: '客观反馈具体发生了什么，避免人身攻击或模糊标签。',
        status: 'ready',
        layout: 'three-column',
        items: [
          { id: 'performance-feedback-review-situation', title: '情境', englishTitle: 'Situation', description: '说明事情发生的具体时间、场景和背景。' },
          { id: 'performance-feedback-review-behavior', title: '行为', englishTitle: 'Behavior', description: '描述可以观察到的具体行为，不推测人格或动机。' },
          { id: 'performance-feedback-review-impact', title: '影响', englishTitle: 'Impact', description: '说明该行为对结果、客户、团队或协作造成的影响。' },
        ],
      },
      {
        id: 'performance-feedback-review-conversation',
        kind: 'steps',
        eyebrow: 'Setup → Next Steps',
        title: '绩效结果五步沟通',
        description: '复盘过去、规划未来，让事实反馈、分歧讨论和后续行动形成闭环。',
        status: 'ready',
        steps: [
          { id: 'performance-feedback-review-step-setup', order: 1, title: '开场', englishTitle: 'Setup', task: '营造建设性氛围，说明沟通目的是复盘过去、规划未来。' },
          { id: 'performance-feedback-review-step-self-evaluation', order: 2, title: '员工自评', englishTitle: 'Self-Evaluation', task: '请员工先分享最骄傲的贡献和遇到的挑战，充分倾听。', note: '区分“动作忙碌”与真实业务产出，也帮助高绩效者提炼可复制的方法。' },
          { id: 'performance-feedback-review-step-manager-view', order: 3, title: '主管反馈', englishTitle: "Manager's View", task: '基于 People Dialogs 中的日常事实记录，用 SBI 反馈贡献与差距。', note: '赞美要具体；建设性反馈要直接、严肃并带有改进方案。' },
          { id: 'performance-feedback-review-step-alignment', order: 4, title: '对焦共识', englishTitle: 'Alignment', task: '讨论主管评估与员工自评不一致之处，对齐未来期望和改进方向。', note: '不强求对过去的判断百分之百一致。' },
          { id: 'performance-feedback-review-step-next', order: 5, title: '收尾跟进', englishTitle: 'Next Steps', task: '明确下一阶段行动点，感谢贡献，并将共识记录在系统中。' },
        ],
      },
    ],
  },
  {
    order: 4,
    number: '04',
    sourceChapter: 4,
    slug: 'consequence-management',
    path: `${PERFORMANCE_MANAGEMENT_BASE_PATH}/consequence-management`,
    title: '结果管理',
    englishTitle: 'Consequence Management',
    tagline: '让差异化结果连接到合适的发展与管理动作',
    summary: '理解绩效结果与薪酬、奖金及发展措施的关联视图，并通过绩效×潜力九宫格讨论差异化发展。',
    objective: '基于事实选择合适的认可、发展或改进方向，同时严格区分指南建议与正式政策。',
    keywords: ['结果管理', 'Consequence Management', 'ASR', 'Annual Bonus', '九宫格', 'Potential', 'High Performance', 'Solid Performance'],
    status: 'policy-review',
    draftNote: 'ASR、奖金、分布比例及低绩效后果属于待审核政策内容；本页不展示草稿中的具体系数或强制规则。',
    sections: [
      {
        id: 'consequence-management-policy-linkage',
        kind: 'table',
        eyebrow: 'Performance Consequences',
        title: '绩效结果的关联视图',
        description: '草稿将绩效结果与 ASR、年终奖金及职业发展/绩效管理措施连接，但具体规则尚不能作为正式依据。',
        status: 'policy-review',
        draftNote: '所有比例、系数、适用团队规模和例外条件均须以最新正式政策为准。',
        caption: '仅用于理解关联关系，不代表计算或决策规则',
        columns: [
          { key: 'area', label: '关联领域' },
          { key: 'draft-direction', label: '草稿中的方向' },
          { key: 'use', label: '当前使用方式' },
        ],
        rows: [
          { id: 'consequence-management-asr', cells: ['ASR', '绩效结果作为相关决策输入之一', '不展示增幅或分布；查询正式政策'] },
          { id: 'consequence-management-bonus', cells: ['Annual Bonus', '评级与奖金输入存在关联', '不展示奖金系数；待政策审核'] },
          { id: 'consequence-management-development', cells: ['发展措施', '高绩效加速发展、坚实绩效定向发展、低绩效提供改进支持', '结合员工情况与正式流程讨论'] },
        ],
      },
      {
        id: 'consequence-management-nine-box',
        kind: 'matrix',
        eyebrow: 'Performance × Potential',
        title: '绩效 × 潜力九宫格',
        description: '九宫格帮助经理在人才讨论中区分当前绩效与未来潜力，不应用单一标签替代事实和校准。',
        status: 'draft',
        draftNote: '九宫格标签来自原稿图表；最终人才定位与措施需结合正式人才政策及校准讨论。',
        xAxis: { label: '绩效评估', lowLabel: '低', highLabel: '高' },
        yAxis: { label: '潜力评估', lowLabel: '低', highLabel: '高' },
        cells: [
          { id: 'consequence-management-cell-talent-risk', x: 1, y: 1, title: 'Talent Risk', description: '低绩效、低潜力', status: 'policy-review' },
          { id: 'consequence-management-cell-change-needed-medium', x: 1, y: 2, title: 'Change Needed', description: '低绩效、中潜力', status: 'policy-review' },
          { id: 'consequence-management-cell-questionmark', x: 1, y: 3, title: 'Questionmark', description: '低绩效、高潜力', status: 'policy-review' },
          { id: 'consequence-management-cell-change-needed-solid', x: 2, y: 1, title: 'Change Needed', description: '中等绩效、低潜力', status: 'draft' },
          { id: 'consequence-management-cell-solid-contributor', x: 2, y: 2, title: 'Solid Contributor', description: '岗位匹配、稳定贡献', status: 'draft' },
          { id: 'consequence-management-cell-future-talent', x: 2, y: 3, title: 'Future Talent', description: '具备迈向下一步的较高潜力', status: 'draft' },
          { id: 'consequence-management-cell-experienced-professional', x: 3, y: 1, title: 'Experienced Professional', description: '在专业领域有经验的高绩效贡献者', status: 'draft' },
          { id: 'consequence-management-cell-key-player', x: 3, y: 2, title: 'Key Player', description: '在当前岗位保持高绩效', status: 'draft' },
          { id: 'consequence-management-cell-top-talent', x: 3, y: 3, title: 'Top Talent', description: '持续卓越绩效并展现高潜力', status: 'draft' },
        ],
      },
      {
        id: 'consequence-management-high-performance',
        kind: 'cards',
        eyebrow: 'Accelerate & Expand',
        title: '高绩效员工的发展方向',
        description: '核心策略是给予空间、加速发展，并根据绩效与潜力定位选择不同的成长路径。',
        status: 'draft',
        draftNote: '人才池、继任、轮岗、晋升与 C&B 相关动作均需按正式流程评估和审批。',
        layout: 'three-column',
        items: [
          {
            id: 'consequence-management-top-talent',
            title: 'Top Talent',
            description: '持续卓越绩效并具有较高潜力，可讨论加速发展与更广职责。',
            bullets: ['通过发展对话了解职业方向。', '形成并跟踪个人发展计划（IDP）。', '考虑具有挑战性的战略项目。'],
          },
          {
            id: 'consequence-management-key-player',
            title: 'Key Player',
            description: '在当前岗位保持高绩效，重点是认可、保留并继续发展。',
            bullets: ['肯定其在当前岗位的关键贡献。', '结合个人意愿讨论下一步发展。'],
          },
          {
            id: 'consequence-management-future-talent',
            title: 'Future Talent',
            description: '展现迈向下一步的潜力，可讨论轮岗、角色拓展与持续跟踪。',
            bullets: ['开展发展对话。', '跟踪约定的发展措施。', '结合业务需要考虑轮岗或角色拓展。'],
          },
        ],
      },
      {
        id: 'consequence-management-solid-performance',
        kind: 'cards',
        eyebrow: 'Targeted Development',
        title: '坚实贡献者的发展方向',
        description: '稳定可靠的贡献者是组织骨干，发展重点应结合岗位匹配、能力差距与个人意愿。',
        status: 'draft',
        draftNote: '原稿 4.4 的“核心策略”尚未完成；以下只整理图表中已列出的发展建议。',
        layout: 'two-column',
        items: [
          {
            id: 'consequence-management-experienced-professional',
            title: 'Experienced Professional',
            description: '在特定领域具备专业经验，可通过反馈对话澄清角色期望，并围绕差距发展能力。',
          },
          {
            id: 'consequence-management-solid-contributor',
            title: 'Solid Contributor',
            description: '岗位匹配且稳定贡献，可鼓励承担更具挑战的任务或解决新问题。',
            bullets: ['提供定向能力培训。', '通过绩效辅导持续提升结果。'],
          },
        ],
      },
      {
        id: 'consequence-management-under-performance-link',
        kind: 'callout',
        eyebrow: 'Coaching & Consequence',
        title: '低绩效管理进入第 5 维',
        description: '涉及改进计划、转岗、合同或退出等动作时，需要更完整的事实记录与合规协同。',
        status: 'policy-review',
        draftNote: '不得根据本草稿直接作出薪酬、PIP 或劳动关系决定。',
        tone: 'policy',
        body: '请继续查看“05 低绩效管理”，并在采取正式行动前与 HRBP；涉及劳动关系时同时与 Legal 确认。',
      },
    ],
  },
  {
    order: 5,
    number: '05',
    sourceChapter: 5,
    slug: 'under-performance-management',
    path: `${PERFORMANCE_MANAGEMENT_BASE_PATH}/under-performance-management`,
    title: '低绩效管理',
    englishTitle: 'Under Performance Management',
    tagline: '基于事实尽早介入，以支持改进和合规流程并行',
    summary: '区分 4/5 级沟通重点，诊断 Can’t do / Won’t do，以紧密辅导、改进计划和持续记录支持员工改善。',
    objective: '在保持同理心的同时清晰说明差距、期望与行动，并让所有正式后果经过 HRBP/Legal 确认。',
    keywords: ['低绩效', 'Under Performance', 'Empathy', 'Firmness', 'Actionable', "Can't do", "Won't do", 'Close Coaching', 'PIP', '30/60/90'],
    status: 'draft',
    draftNote: '原稿第 5 章正文仅写“略”；本页内容只从第 3、4 章已有低绩效材料重新归集，未补写新的政策规则。',
    sections: [
      {
        id: 'under-performance-management-levels',
        kind: 'comparison',
        eyebrow: 'Level 4 / Level 5',
        title: '先区分表现，再选择沟通重点',
        description: '两类情形都需要事实依据和及时介入，但差距程度、支持强度和合规要求不同。',
        status: 'policy-review',
        draftNote: '评级归属和正式动作须以校准结果及有效政策为准。',
        bridgeLabel: '事实差距与支持强度不同',
        columns: [
          {
            id: 'under-performance-management-level-four',
            title: '4 · Partially Achieved',
            englishTitle: '有待改进者',
            description: '部分目标未达成或表现不够稳定，沟通重点是诊断与辅导。',
            items: ['识别能力或关键职责上的差距。', '提供更具体的指导并紧密跟进。'],
          },
          {
            id: 'under-performance-management-level-five',
            title: '5 · Not Achieved',
            englishTitle: '未达成者',
            description: '存在显著的技能、产出或行为差距，需要艰难对话与合规协同。',
            items: ['尽早邀请 HRBP 参与。', '正式 PIP 与劳动关系动作须按政策确认。'],
          },
        ],
      },
      {
        id: 'under-performance-management-principles',
        kind: 'cards',
        eyebrow: 'Communication Principles',
        title: '低绩效沟通的三个支点',
        description: '既不回避问题，也不把人等同于问题。',
        status: 'ready',
        layout: 'three-column',
        items: [
          { id: 'under-performance-management-empathy', title: '同理心', englishTitle: 'Empathy', description: '倾听员工的感受和处境，保持尊重并创造可坦诚交流的空间。' },
          { id: 'under-performance-management-firmness', title: '坚定原则', englishTitle: 'Firmness', description: '用事实说明差距与岗位期望，不模糊关键反馈。' },
          { id: 'under-performance-management-actionable', title: '明确行动', englishTitle: 'Actionable', description: '把反馈落到具体、可跟进的改进行动与支持上。' },
        ],
      },
      {
        id: 'under-performance-management-diagnosis',
        kind: 'cards',
        eyebrow: "Can't do / Won't do",
        title: '共同探寻差距原因',
        description: '先诊断再选择支持方式，避免只凭主观印象判断员工动机。',
        status: 'ready',
        layout: 'two-column',
        items: [
          {
            id: 'under-performance-management-cant-do',
            title: '能力或技能不足',
            englishTitle: "Can't do",
            description: '确认知识、技能、资源、岗位匹配或工作方法上是否存在阻碍。',
            bullets: ['提供更具体的工作指导。', '讨论培训、辅导、资源或角色匹配。'],
          },
          {
            id: 'under-performance-management-wont-do',
            title: '动机或态度问题',
            englishTitle: "Won't do",
            description: '通过事实和开放提问了解承诺、意愿与行为选择，避免直接贴标签。',
            bullets: ['清晰重申岗位期望。', '约定可观察的行为与跟进方式。'],
          },
        ],
      },
      {
        id: 'under-performance-management-conversation',
        kind: 'steps',
        eyebrow: 'Low Performance Conversation',
        title: '低绩效五步谈话',
        description: '以事实开场，共同诊断原因，再把期望、支持和跟进记录清楚。',
        status: 'ready',
        steps: [
          { id: 'under-performance-management-step-facts', order: 1, title: '说明事实', task: '用 SBI 客观指出具体差距，不进行人身攻击。' },
          { id: 'under-performance-management-step-listen', order: 2, title: '倾听视角', task: '请员工说明其理解、困难和相关背景，确认信息是否完整。' },
          { id: 'under-performance-management-step-diagnose', order: 3, title: '共同诊断', task: '区分能力技能、资源与岗位匹配问题，或动机与行为问题。' },
          { id: 'under-performance-management-step-plan', order: 4, title: '明确计划', task: '共同制定具体、可实现的改进目标，并说明经理可提供的支持。' },
          { id: 'under-performance-management-step-follow-up', order: 5, title: '持续跟进', task: '约定检查频次，记录进展、反馈和支持；草稿建议每周同步。', prompt: '“我们要坦诚面对目前的差距，并共同制定具体的改进计划；我会持续与你同步进展。”' },
        ],
      },
      {
        id: 'under-performance-management-pip-timeline',
        kind: 'flow',
        eyebrow: '30 / 60 / 90',
        title: '改进计划时间轴（草稿示意）',
        description: '原稿提出 30/60/90 天的 PIP 计划；具体周期、目标、启动条件和表单必须由正式流程确认。',
        status: 'policy-review',
        draftNote: '以下仅用于组织检查点，不构成 PIP 政策或自动触发规则。',
        orientation: 'horizontal',
        items: [
          { id: 'under-performance-management-pip-start', title: '启动前', description: '整理事实，与 HRBP 确认适用流程、周期、角色和记录要求。' },
          { id: 'under-performance-management-pip-thirty', title: '30 天（示意）', description: '按已确认计划检查阶段目标、支持是否到位以及事实证据。' },
          { id: 'under-performance-management-pip-sixty', title: '60 天（示意）', description: '复盘改善趋势，继续记录反馈并根据正式流程调整支持。' },
          { id: 'under-performance-management-pip-ninety', title: '90 天（示意）', description: '完成阶段复评；任何后续处理均按正式政策与 HRBP/Legal 意见执行。' },
        ],
      },
      {
        id: 'under-performance-management-weekly-check',
        kind: 'cards',
        eyebrow: 'Close Coaching',
        title: '每次跟进都留下清晰共识',
        description: '增加 1:1 频次并提供更具象的日常指导，让支持、反馈和进展可追踪。',
        status: 'ready',
        layout: 'four-column',
        items: [
          { id: 'under-performance-management-weekly-evidence', title: '事实', description: '本周期出现了哪些可观察的结果与行为？' },
          { id: 'under-performance-management-weekly-progress', title: '进展', description: '与约定目标相比，哪些已改善、哪些仍有差距？' },
          { id: 'under-performance-management-weekly-support', title: '支持', description: '经理和团队已提供什么，还需要哪些资源或指导？' },
          { id: 'under-performance-management-weekly-record', title: '记录', description: '下一步行动、责任人和检查时间是否已形成共同记录？' },
        ],
      },
      {
        id: 'under-performance-management-decision-flow',
        kind: 'flow',
        eyebrow: 'Coaching → Review → Next Action',
        title: '从辅导到复评的决策路径',
        description: '先支持改善，再根据持续事实评估岗位匹配与后续路径。',
        status: 'policy-review',
        draftNote: '转岗、PIP、合同与退出等正式动作必须遵循有效政策，并由 HRBP/Legal 确认。',
        orientation: 'horizontal',
        items: [
          { id: 'under-performance-management-flow-coaching', title: '紧密辅导', description: '明确差距、目标和支持，增加反馈与 1:1 频次。' },
          { id: 'under-performance-management-flow-review', title: '阶段复评', description: '依据记录判断是否出现持续、可验证的改善。' },
          { id: 'under-performance-management-flow-role-fit', title: '岗位匹配讨论', description: '若涉及能力或角色错配，与员工及 HRBP 讨论合适岗位的可能性。' },
          { id: 'under-performance-management-flow-policy', title: '正式后续流程', description: '如仍无改善，依据正式政策和 HRBP/Legal 意见决定下一步。' },
        ],
      },
      {
        id: 'under-performance-management-policy-note',
        kind: 'callout',
        eyebrow: 'Policy Guardrail',
        title: '采取正式行动前',
        description: '草稿中的后果示例不等于已生效政策。',
        status: 'policy-review',
        tone: 'policy',
        body: '涉及奖金限制、晋升资格、强制 PIP、转岗、合同不续签或解除等事项，必须以正式政策为准，并提前与 HRBP；涉及劳动关系时同时与 Legal 确认。',
        bullets: ['保留客观、及时、完整的事实记录。', '不要承诺草稿中的具体期限、比例或结果。'],
      },
    ],
  },
] as const satisfies readonly PerformanceManagementDimension[];

function assertNever(value: never): never {
  throw new Error(`Unsupported performance-management section: ${JSON.stringify(value)}`);
}

function validateSection(section: PerformanceManagementSection, dimensionSlug: string): void {
  if (!section.id.startsWith(`${dimensionSlug}-`) || !/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(section.id)) {
    throw new Error(
      `Performance-management section "${section.id}" must be a kebab-case ID prefixed by "${dimensionSlug}-".`,
    );
  }
  if (!section.title.trim()) {
    throw new Error(`Performance-management section "${section.id}" is missing a title.`);
  }

  switch (section.kind) {
    case 'cards':
      if (section.items.length === 0) {
        throw new Error(`Cards section "${section.id}" must contain at least one item.`);
      }
      break;
    case 'comparison':
      if (section.columns.some((column) => column.items.length === 0)) {
        throw new Error(`Comparison section "${section.id}" must populate both columns.`);
      }
      break;
    case 'steps':
      if (section.steps.length === 0 || section.steps.some((step, index) => step.order !== index + 1)) {
        throw new Error(`Steps section "${section.id}" must use consecutive one-based ordering.`);
      }
      break;
    case 'table':
      if (
        section.columns.length === 0
        || section.rows.length === 0
        || section.rows.some((row) => row.cells.length !== section.columns.length)
      ) {
        throw new Error(`Table section "${section.id}" must have non-empty, column-aligned rows.`);
      }
      break;
    case 'flow':
      if (section.items.length === 0) {
        throw new Error(`Flow section "${section.id}" must contain at least one item.`);
      }
      break;
    case 'matrix': {
      const coordinates = new Set(section.cells.map((cell) => `${cell.x}:${cell.y}`));
      if (section.cells.length !== 9 || coordinates.size !== 9) {
        throw new Error(`Matrix section "${section.id}" must contain each 3×3 coordinate exactly once.`);
      }
      break;
    }
    case 'rating': {
      const levels = new Set(section.items.map((item) => item.level));
      if (section.items.length !== 5 || levels.size !== 5 || [1, 2, 3, 4, 5].some((level) => !levels.has(level as 1 | 2 | 3 | 4 | 5))) {
        throw new Error(`Rating section "${section.id}" must contain levels 1–5 exactly once.`);
      }
      break;
    }
    case 'callout':
      if (!section.body.trim()) {
        throw new Error(`Callout section "${section.id}" is missing its body.`);
      }
      break;
    default:
      assertNever(section);
  }
}

export function validatePerformanceManagementOverviewContent(
  content: PerformanceManagementOverviewContent,
): PerformanceManagementOverviewContent {
  if (
    content.order !== 1
    || content.number !== '01'
    || content.sourceChapter !== PERFORMANCE_MANAGEMENT_OVERVIEW_SOURCE_CHAPTER
    || content.slug !== 'overview'
    || content.path !== `${PERFORMANCE_MANAGEMENT_BASE_PATH}/overview`
    || content.englishTitle !== 'Overview'
    || !content.title.trim()
    || !content.summary.trim()
    || !content.objective.trim()
    || content.keywords.length === 0
    || content.sections.length === 0
  ) {
    throw new Error('Performance-management Overview is missing required chapter or display content.');
  }

  const sectionIds = new Set<string>();
  content.sections.forEach((section) => {
    if (sectionIds.has(section.id)) {
      throw new Error(`Duplicate performance-management Overview section ID "${section.id}".`);
    }
    validateSection(section, 'overview');
    sectionIds.add(section.id);
  });

  return content;
}

export function validatePerformanceManagementDimensions(
  dimensions: readonly PerformanceManagementDimension[],
): readonly PerformanceManagementDimension[] {
  if (dimensions.length !== PERFORMANCE_MANAGEMENT_DIMENSION_SLUGS.length) {
    throw new Error(
      `Performance management must contain exactly ${PERFORMANCE_MANAGEMENT_DIMENSION_SLUGS.length} dimensions.`,
    );
  }

  const paths = new Set<string>();
  const sectionIds = new Set<string>();

  dimensions.forEach((dimension, index) => {
    const expectedOrder = index + 1;
    const expectedSlug = PERFORMANCE_MANAGEMENT_DIMENSION_SLUGS[index];
    const expectedSourceChapter = PERFORMANCE_MANAGEMENT_SOURCE_CHAPTERS[index];
    const expectedNumber = String(expectedSourceChapter).padStart(2, '0');
    const expectedPath = `${PERFORMANCE_MANAGEMENT_BASE_PATH}/${expectedSlug}`;

    if (
      dimension.order !== expectedOrder
      || dimension.number !== expectedNumber
      || dimension.sourceChapter !== expectedSourceChapter
      || dimension.slug !== expectedSlug
      || dimension.path !== expectedPath
    ) {
      throw new Error(
        `Performance-management dimension ${index + 1} must map source chapter ${expectedSourceChapter} to ${expectedNumber} / ${expectedSlug} / ${expectedPath}.`,
      );
    }
    if (
      !dimension.title.trim()
      || !dimension.englishTitle.trim()
      || !dimension.summary.trim()
      || !dimension.objective.trim()
      || dimension.keywords.length === 0
    ) {
      throw new Error(`Performance-management dimension "${dimension.slug}" is missing display content.`);
    }
    if (paths.has(dimension.path)) {
      throw new Error(`Duplicate performance-management path "${dimension.path}".`);
    }
    if (dimension.sections.length === 0) {
      throw new Error(`Performance-management dimension "${dimension.slug}" has no sections.`);
    }

    paths.add(dimension.path);
    dimension.sections.forEach((section) => {
      if (sectionIds.has(section.id)) {
        throw new Error(`Duplicate performance-management section ID "${section.id}".`);
      }
      validateSection(section, dimension.slug);
      sectionIds.add(section.id);
    });
  });

  return dimensions;
}

validatePerformanceManagementOverviewContent(performanceManagementOverviewContent);

export const performanceManagementDimensions = validatePerformanceManagementDimensions(
  performanceManagementDimensionData,
);

export const performanceManagementDimensionBySlug: Readonly<
  Record<PerformanceManagementDimensionSlug, PerformanceManagementDimension>
> = Object.freeze(
  Object.fromEntries(
    performanceManagementDimensions.map((dimension) => [dimension.slug, dimension]),
  ) as Record<PerformanceManagementDimensionSlug, PerformanceManagementDimension>,
);

export function isPerformanceManagementDimensionSlug(
  value: string | undefined,
): value is PerformanceManagementDimensionSlug {
  return Boolean(
    value
    && PERFORMANCE_MANAGEMENT_DIMENSION_SLUGS.includes(value as PerformanceManagementDimensionSlug),
  );
}

export function getPerformanceManagementDimension(
  slug: string | undefined,
): PerformanceManagementDimension | undefined {
  return isPerformanceManagementDimensionSlug(slug)
    ? performanceManagementDimensionBySlug[slug]
    : undefined;
}

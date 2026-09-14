export type ToolkitPageSlug =
  | 'strategy-analysis'
  | 'coaching-goals'
  | 'feedback-assessment'
  | 'development-frameworks';

export type ToolkitIconName =
  | 'strategy'
  | 'analysis'
  | 'alignment'
  | 'skills'
  | 'goal'
  | 'coaching'
  | 'feedback'
  | 'assessment'
  | 'team'
  | 'leadership'
  | 'development';

export interface ToolkitDetailItem {
  title: string;
  detail: string;
}

export interface ToolkitDetailSection {
  title: string;
  description: string;
  items: readonly ToolkitDetailItem[];
}

export interface ToolkitMethodDetail {
  subtitle: string;
  overviewTitle: string;
  overview: readonly string[];
  frameworkTitle: string;
  framework: readonly ToolkitDetailItem[];
  exampleSection?: ToolkitDetailSection;
  processTitle: string;
  process: readonly ToolkitDetailItem[];
  benefitsSection?: ToolkitDetailSection;
  useCases: readonly string[];
  pitfalls: readonly ToolkitDetailItem[];
  managerApplication: readonly string[];
  keyTakeaway: string;
}

export interface ToolkitTool {
  id: string;
  path: string;
  title: string;
  summary: string;
  summaryEnglish: string;
  application: string;
  icon: ToolkitIconName;
  detail: ToolkitMethodDetail;
}

export interface ToolkitPageConfig {
  slug: ToolkitPageSlug;
  path: string;
  navLabel: string;
  navLabelEnglish: string;
  title: string;
  titleEnglish: string;
  summary: string;
  summaryEnglish: string;
  heroImage: string;
  heroImageAlt: string;
  introTitle: string;
  introParagraphs: readonly string[];
  toolsHeading: string;
  toolsHeadingEnglish: string;
  tools: readonly ToolkitTool[];
  workflow: readonly ToolkitDetailItem[];
  ctaTitle: string;
  ctaDescription: string;
  accent: string;
  accentSoft: string;
}

const LANDING_ASSET = '/assets/landing';
const toolPath = (category: ToolkitPageSlug, tool: string) => `/solutions/${category}/${tool}`;

export const mckinseySevenSArticle = {
  readingTime: '约 12 分钟阅读',
  diagnostics: {
    title: '用七组问题诊断组织是否真正对齐',
    description: '先分别判断每个要素是否清晰、有效，再检查它与其余要素是否指向同一战略。评分只是起点，真正的诊断来自要素之间的关系。',
    items: [
      {
        element: 'Strategy',
        label: '战略',
        category: 'hard',
        questions: ['各层级是否能用一致语言说明战略？', '预算、人才和管理注意力是否投向战略重点？', '组织选择的取胜方式是否具有清晰差异？'],
      },
      {
        element: 'Structure',
        label: '结构',
        category: 'hard',
        questions: ['当前结构是在支持战略，还是持续制造协作摩擦？', '关键决策权与责任边界是否明确？', '跨职能、跨地区和跨业务单元的协调机制是否有效？'],
      },
      {
        element: 'Systems',
        label: '系统',
        category: 'hard',
        questions: ['规划、预算和绩效机制是否强化战略重点？', '信息系统是否为决策提供及时可靠的数据？', '日常流程是在加速执行，还是让工作反复等待？'],
      },
      {
        element: 'Shared Values',
        label: '共同价值观',
        category: 'core',
        questions: ['员工能否说清组织真正重视什么？', '被奖励的行为是否与公开价值观一致？', '不同团队和地区是否遵循相容的工作原则？'],
      },
      {
        element: 'Skills',
        label: '技能',
        category: 'soft',
        questions: ['组织整体最突出的能力是什么？', '未来战略面临哪些关键能力缺口？', '招聘、实践和知识传承能否持续形成新能力？'],
      },
      {
        element: 'Style',
        label: '领导风格',
        category: 'soft',
        questions: ['领导者实际把时间花在哪里？', '真实奖励与口头倡导是否一致？', '决策、授权和沟通方式是否符合战略需要？'],
      },
      {
        element: 'Staff',
        label: '人员',
        category: 'soft',
        questions: ['人才管道是否匹配未来业务需要？', '哪些关键人才正在离开，背后的原因是什么？', '配置、发展和继任机制是否在建立正确能力？'],
      },
    ],
  },
  caseStudy: {
    title: '案例：一次失速的并购整合',
    description: '一家以规模和运营效率见长的工业企业收购了一家技术公司。十二个月后，协同收益远低于计划，关键工程师持续流失，交叉销售也没有形成。7S 诊断显示，问题并不只在组织图。',
    rows: [
      { element: 'Strategy', acquirer: '依靠规模降低成本', target: '以创新支持溢价', misalignment: '两套价值主张并存，销售团队无法向客户解释整合后的重点。' },
      { element: 'Structure', acquirer: '职能层级与集中决策', target: '扁平产品团队与分散授权', misalignment: '产品和工程负责人失去原有决策权，问题被逐级上报。' },
      { element: 'Systems', acquirer: '阶段门与季度计划', target: '敏捷迭代与持续交付', misalignment: '统一流程拉长技术团队的反馈周期，开发速度明显下降。' },
      { element: 'Shared Values', acquirer: '效率、稳定与风险控制', target: '速度、实验与快速学习', misalignment: '一方认为对方官僚，另一方认为对方缺乏纪律，信任持续下降。' },
      { element: 'Skills', acquirer: '制造与供应链优化', target: '软件工程与数据科学', misalignment: '能力本可互补，但缺少联合项目、交叉学习和知识转移机制。' },
      { element: 'Style', acquirer: '自上而下、充分共识后决策', target: '团队自主、偏向快速行动', misalignment: '原有产品负责人等待审批，管理节奏与业务需要不匹配。' },
      { element: 'Staff', acquirer: '长期任职与忠诚导向', target: '股权激励、流动性较高', misalignment: '激励和文化改变后，最关键的技术人才首先离开。' },
    ],
    insight: '最深层的断点位于共同价值观和领导风格。若不先明确双方如何共同工作，仅靠改汇报线和统一流程，很难阻止人才流失或恢复交付速度。',
    actions: [
      '先形成联合工作原则，保留创新速度，同时明确效率与风险边界。',
      '再校准领导风格，为产品团队保留必要授权，并建立共同复盘节奏。',
      '随后设计兼容的系统，让软件与硬件采用适合各自工作的流程并设置接口。',
      '最后调整结构，用已经验证的协作方式决定汇报关系和治理安排。',
    ],
  },
  comparison: {
    title: '7S 与其他组织变革模型如何选择',
    description: '不同模型回答的问题不同。7S 适合寻找组织内部的系统性错位，也可以与面向因果链或个人采纳的模型组合使用。',
    rows: [
      { model: 'McKinsey 7S', focus: '七个内部要素的一致性', level: '组织整体', bestFor: '并购、重组、战略转向和执行失速的综合诊断' },
      { model: 'Burke-Litwin', focus: '从外部环境到个人绩效的因果链', level: '组织到个人', bestFor: '追踪绩效问题的上游驱动因素与连锁影响' },
      { model: 'Nadler-Tushman', focus: '战略、工作、人员与结构之间的契合', level: '组织整体', bestFor: '判断战略与组织设计是否相互匹配' },
      { model: 'ADKAR', focus: '个人采纳变化的连续阶段', level: '个人', bestFor: '支持认知、意愿、知识、能力与强化的落地过程' },
    ],
  },
  presentation: {
    title: '如何把 7S 诊断呈现给决策者',
    description: '好的呈现要让错位、差距和行动顺序一眼可见，而不是把七项观察堆成一份长清单。',
    items: [
      { title: '保留中心关系图', detail: '把 Shared Values 放在中心，并让六个外围要素明确连接，提醒团队任何单项变化都会牵动整个系统。' },
      { title: '并列当前与目标状态', detail: '为每个要素展示当前评分、目标评分和差距，同时标记证据来源，避免把愿望当成现状。' },
      { title: '标记对齐程度与责任', detail: '用一致、部分一致和冲突三种状态标记要素关系，再连接优先行动、负责人和复盘时间。' },
    ],
  },
} as const;

export const toolkitPages = {
  'strategy-analysis': {
    slug: 'strategy-analysis',
    path: '/solutions/strategy-analysis',
    navLabel: '战略与分析',
    navLabelEnglish: 'Strategy & Analysis',
    title: '战略与分析：行动前先看清全局',
    titleEnglish: 'Strategy and analysis: see the full picture before you act',
    summary: '在行动之前看清全局，用结构化分析把分散信息转化为清晰、可解释的决策依据。',
    summaryEnglish: 'Turn scattered information into a clear, explainable view before making important decisions.',
    heroImage: `${LANDING_ASSET}/solution-category-strategy-premium-202609.webp`,
    heroImageAlt: '两名工程师在施工现场共同检查工程进展',
    introTitle: '先建立完整视角，再做关键判断',
    introParagraphs: [
      '复杂决策往往不是缺少信息，而是信息分散、判断标准不一致。战略工具帮助团队把外部环境、内部能力和未来要求放在同一幅图景中。',
      'SWOT 建立全局视图，VRIO 检验优势能否持续，McKinsey 7S 观察组织是否一致，技能差距分析则把未来要求转化为发展优先级。它们彼此互补，而不是相互替代。',
    ],
    toolsHeading: '战略与分析',
    toolsHeadingEnglish: 'Strategy and analysis',
    tools: [
      {
        id: 'swot-analysis',
        path: toolPath('strategy-analysis', 'swot-analysis'),
        title: 'SWOT Analysis',
        summary: '把业务、项目或目标的内部优势与弱点、外部机会与威胁放在同一张矩阵中，形成清晰的决策全景。',
        summaryEnglish: 'Bring internal strengths and weaknesses together with external opportunities and threats in one matrix to create a clear decision overview.',
        application: '适合重大变化前的准备度判断、新项目启动、增长机会评估、业务复盘、风险识别和战略规划。',
        icon: 'analysis',
        detail: {
          subtitle: '在一张四象限矩阵中连接内部现实与外部环境，从分散观察走向有证据、有优先级的行动计划。',
          overviewTitle: '什么是 SWOT 分析？',
          overview: [
            'SWOT 分析是一种用于评估企业、项目或具体目标的战略规划工具。SWOT 分别代表 Strengths（优势）、Weaknesses（弱点）、Opportunities（机会）和 Threats（威胁），帮助团队在投入资源或作出重要决定前建立对当前位置的共同理解。',
            '优势和弱点属于内部因素，通常与能力、资源、流程和既有表现有关；机会和威胁来自外部环境，例如市场趋势、竞争变化、客户需求、技术发展、供应链和监管要求。将内部与外部因素并列比较，可以减少只凭单一视角判断的偏差。',
            '四象限只是信息整理方式，可执行策略才是分析真正的产出。团队需要用证据校验每条观察，选出少数高影响因素，再把优势、弱点、机会和威胁连接到项目计划、风险措施、负责人和时间节点。',
          ],
          frameworkTitle: 'SWOT 的四个构成要素',
          framework: [
            { title: 'Strengths · 优势', detail: '正在发挥作用的内部能力与资源。可以追问：我们最擅长什么？哪些能力具有独特性？客户或员工最认可什么？哪些结果优于竞争者？每项优势都应由数据、案例或稳定行为支持。' },
            { title: 'Weaknesses · 弱点', detail: '妨碍目标实现的内部限制。可以追问：哪些工作表现不佳，原因是什么？什么需要改善？缺少哪些资源、技能或流程支持？与竞争者相比差距在哪里？弱点应具体到可改善的问题。' },
            { title: 'Opportunities · 机会', detail: '可能帮助目标实现的外部有利条件。可以追问：市场或服务是否存在空白？哪些趋势、技术或合作资源可以利用？年度目标带来了什么新空间？竞争者尚未满足哪些需求？' },
            { title: 'Threats · 威胁', detail: '可能造成损失且无法直接控制的外部因素。可以追问：哪些行业变化值得警惕？未来趋势会带来什么风险？竞争者在哪些方面领先？监管、供应链或技术替代会怎样影响结果？' },
          ],
          exampleSection: {
            title: 'SWOT 示例：管理者教练计划',
            description: '假设一家制造企业准备推出跨部门管理者教练计划。矩阵中的每一项都围绕同一个决策目标，并区分组织能够直接改善的内部条件与需要主动响应的外部变化。',
            items: [
              { title: 'Strengths · 优势', detail: '已有成熟的领导力课程、稳定的内部讲师队伍和高层发起人支持；过去的管理者反馈数据也能为项目设计提供基线。' },
              { title: 'Weaknesses · 弱点', detail: '一线经理可投入时间有限，教练能力差异较大，培训结束后的行动记录分散，尚未建立统一的跟进和效果衡量方式。' },
              { title: 'Opportunities · 机会', detail: '组织转型提高了个性化发展的需求，数字化教练工具正在成熟，跨部门人才流动也为真实练习和同伴学习创造了场景。' },
              { title: 'Threats · 威胁', detail: '短期经营压力可能挤压学习时间，多个转型项目争夺同一批管理者资源；如果隐私边界不清，还可能降低员工参与和坦诚表达的意愿。' },
            ],
          },
          processTitle: '如何完成 SWOT 分析：五个步骤',
          process: [
            { title: '明确目标并审视内部因素', detail: '先界定分析对象、决策问题和时间范围，再基于绩效数据、客户反馈、流程表现和资源现状识别优势与弱点。优先处理能够直接改善或在短期内采取行动的内部事项。' },
            { title: '评估外部因素', detail: '研究竞争者、市场趋势、客户需求、技术、政策和供应链变化，识别机会与威胁。外部因素无法直接控制，但团队可以通过预测、提升适应速度和调整方案来降低影响。' },
            { title: '组织跨职能头脑风暴', detail: '邀请能够代表不同业务视角的参与者，在清晰的问题和规则下补充四个象限。鼓励先独立提交观点，再共同讨论，避免少数人的判断主导整个分析。' },
            { title: '评估并排列优先级', detail: '合并重复观点，检查证据，并按影响、紧迫性、可行性和团队能力对机会与风险评分。聚焦少数真正影响决策的因素，而不是保留一份过长且没有重点的清单。' },
            { title: '把结论转化为行动', detail: '将优先事项写入项目计划或实施方案，明确行动、负责人、时间节点和衡量信号。利用优势抓住机会，补强会放大威胁的弱点，并定期更新矩阵与行动状态。' },
          ],
          benefitsSection: {
            title: 'SWOT 能带来什么？',
            description: 'SWOT 的价值在于把不同来源的观察压缩为团队可以共同讨论的决策图景，并把现状评估连接到优先级、风险与执行。',
            items: [
              { title: '快速建立共同视图', detail: '用同一套分类并列呈现内部能力与外部条件，帮助跨职能成员在较短时间内理解当前位置和关键分歧。' },
              { title: '连接战略与行动', detail: '将组织真正具备的优势和需要改善的弱点，与市场机会和现实威胁结合，减少脱离能力基础的战略选择。' },
              { title: '提前识别风险', detail: '在投入资源前看见外部威胁及会放大威胁的内部弱点，并把关键事项连接到风险登记、范围调整或应对计划。' },
              { title: '提高适应变化的速度', detail: '定期更新矩阵可以及时发现客户、竞争、技术或组织能力的变化，帮助团队调整优先级，而不是沿用已经过时的判断。' },
            ],
          },
          useCases: [
            '实施重大组织、流程或产品变化之前',
            '启动新项目、新业务或新市场计划时',
            '寻找增长机会、改进方向或竞争优势时',
            '需要从多个视角全面评估业务或团队表现时',
          ],
          pitfalls: [
            { title: '依赖主观判断', detail: '个人印象容易引入偏差。为每个观点补充数据、客户反馈、具体案例或共同观察，并标记仍需验证的假设。' },
            { title: '使用模糊表述', detail: '“品牌不错”或“竞争激烈”无法指导行动。说明具体对象、影响、证据和适用范围，让团队能够判断优先级。' },
            { title: '混淆内部与外部因素', detail: '能力、资源和流程通常属于内部因素；市场、竞争、政策和技术变化通常属于外部因素。分类准确才能匹配正确的应对方式。' },
            { title: '把 SWOT 当作一次性练习', detail: '环境和组织能力会持续变化。应在关键里程碑、季度复盘或重大变化后重新检查四个象限。' },
            { title: '分析结束却没有下一步', detail: 'SWOT 只有在形成可执行策略时才有价值。每个优先事项都应连接到行动、负责人、期限和衡量标准。' },
          ],
          managerApplication: [
            '在团队或员工发展对话中，先明确要支持的决策，再分别收集优势与弱点的内部证据，以及机会与威胁的外部信号。让员工先独立思考，再通过对话校验观点，可以减少经理先入为主的影响。',
            '经理的重点不是替团队填满矩阵，而是追问证据、区分事实与假设、帮助排列优先级，并把结论落实到少量清晰行动。对无法控制的外部威胁，应讨论可调整的响应方式，而不是停留在担忧本身。',
            '复盘时回到行动计划，检查优势是否得到利用、弱点是否改善、机会是否仍然存在，以及威胁是否发生变化。SWOT 应成为动态决策工具，而不是会议结束后被归档的文档。',
          ],
          keyTakeaway: 'SWOT 的第五个关键要素是可执行策略：只有把四象限的证据转化为有负责人、有期限、可衡量的行动，分析才真正完成。',
        },
      },
      {
        id: 'vrio-analysis',
        path: toolPath('strategy-analysis', 'vrio-analysis'),
        title: 'VRIO Analysis',
        summary: '用价值、稀缺性、难模仿性与组织承接能力四项连续检验，识别资源真正能够形成的竞争结果。',
        summaryEnglish: 'Test value, rarity, inimitability, and organizational support in sequence to identify the competitive outcome a resource can sustain.',
        application: '适合核心资源盘点、竞争优势诊断、战略投资取舍，以及关键能力的保护与规模化。',
        icon: 'strategy',
        detail: {
          subtitle: '不要只问组织拥有什么；依次判断一项资源是否有价值、足够稀缺、难以复制，并且已经被组织有效利用。',
          overviewTitle: '什么是 VRIO 分析？',
          overview: [
            'VRIO 是一套面向组织内部的战略评估框架，用来检查资源和能力能否支撑竞争优势。四个字母分别代表 Value（价值）、Rarity（稀缺性）、Imitability（难模仿性）和 Organization（组织承接能力）。分析对象既可以是设备、数据和资本，也可以是品牌、关系、流程、经验与能力组合。',
            '四项问题构成一条连续判断路径，而不是四个彼此独立的评分项。资源先要创造价值，再检验拥有者是否稀少、竞争者是否难以复制或替代，最后判断组织是否具备把它转化为结果的结构与机制。任一环节未通过，竞争结果都会随之改变。',
            '结论必须建立在客户价值、成本、市场机会、外部威胁、竞争者行动和替代方案等证据上。VRIO 不是一次性的资源清单；随着技术、人才流动和行业结构变化，原本稀缺或难模仿的能力也需要重新评估。',
          ],
          frameworkTitle: '沿着 V → R → I → O 逐项检验',
          framework: [
            { title: 'Value · 价值', detail: '核心问题：它是否帮助组织把握机会、降低威胁或改善客户结果？同时比较收益与投入，并检查客户、供应商、竞争对手及替代方案带来的影响。无法创造净价值的资源更可能是成本负担。' },
            { title: 'Rarity · 稀缺性', detail: '核心问题：有多少当前或潜在竞争者拥有同等资源或相同能力组合？稀缺不等于偶然短缺，还要确认组织能否持续、稳定地获得或保有它。广泛可得的能力通常只能支持竞争均势。' },
            { title: 'Imitability · 难模仿性', detail: '核心问题：竞争者复制它或找到等效替代方案，需要付出多大的资金、时间与组织成本？独特历史、隐性知识、复杂关系、因果模糊和能力协同，通常会让模仿变得更慢、更贵。' },
            { title: 'Organization · 组织承接能力', detail: '核心问题：组织是否已经准备好充分利用它？检查治理结构、角色责任、预算、流程、激励，以及销售、交付、供应和支持系统。前三项成立但组织未就绪，优势仍会停留在潜力阶段。' },
          ],
          exampleSection: {
            title: 'VRIO 示例：跨工厂的工业工程能力',
            description: '假设一家制造企业希望判断其跨工厂工业工程能力是否值得继续重点投资。以下示例展示如何把四个判断建立在可观察证据上。',
            items: [
              { title: 'Value · 价值', detail: '该能力持续降低换线时间与材料浪费，并改善新品导入速度、质量稳定性和交付表现；收益能够由运营数据与客户结果验证，因此通过价值检验。' },
              { title: 'Rarity · 稀缺性', detail: '精益工具并不稀缺，但能够跨工厂复用数据、专家经验与协作流程的完整体系，仅被少数同类竞争者掌握；真正稀缺的是能力组合，而非某一个方法。' },
              { title: 'Imitability · 难模仿性', detail: '竞争者可以买到相同软件和设备，却难以快速复制多年积累的隐性经验、现场关系、历史数据与工作惯例；复制整套体系需要较长时间和显著组织成本。' },
              { title: 'Organization · 组织承接能力', detail: '企业已经设置跨工厂治理角色、专家网络、知识复用流程、预算与激励，并把能力嵌入新品和改善项目；资源因此能够被持续部署，而不是依赖少数个人。' },
            ],
          },
          processTitle: '如何完成 VRIO 分析：五个步骤',
          process: [
            { title: '明确分析目标与边界', detail: '先说明要支持的战略决定、业务范围、目标客户和比较对象。VRIO 的结论取决于具体市场与竞争环境，同一资源在不同场景中的价值和稀缺程度可能完全不同。' },
            { title: '盘点资源并收集证据', detail: '列出与目标直接相关的有形资源、知识经验、流程方法、技术、品牌关系和能力组合。为每项资源补充绩效数据、客户反馈、成本信息和竞争比较，避免只凭内部印象判断。' },
            { title: '按 V、R、I、O 顺序检验', detail: '先判断价值，再依次判断稀缺性、难模仿性和组织承接能力。每一步都记录证据、假设和不确定性；某项未通过时，明确它为何未通过以及是否可以被改善。' },
            { title: '确定当前竞争结果', detail: '不具价值通常意味着竞争劣势；有价值但不稀缺意味着竞争均势；有价值且稀缺但容易模仿意味着暂时优势；前三项满足但组织未就绪，属于尚未兑现的潜在优势；四项全部满足才可能形成持续优势。' },
            { title: '制定资源行动方案', detail: '减少或修复没有价值的投入，以更高效率维持基础能力，保护稀缺且难模仿的资源，并为潜在优势补齐角色、流程、预算和激励。为每项行动明确负责人、期限和衡量指标。' },
          ],
          benefitsSection: {
            title: '从四个答案读出五种竞争结果',
            description: 'VRIO 的价值在于把四项判断连接到明确的战略含义。沿着 V、R、I、O 从左到右阅读，在第一个“否”出现的位置即可识别当前结果与行动重点。',
            items: [
              { title: '竞争劣势', detail: '资源没有创造净价值。减少、重构或退出相关投入，并优先修复它造成的成本与风险。' },
              { title: '竞争均势', detail: '资源有价值，但市场中并不稀缺。以更高效率维持行业必备能力，避免为同质资源支付过高溢价。' },
              { title: '暂时竞争优势', detail: '资源有价值且稀缺，但复制或替代成本不高。加快商业化和规模化，同时持续构建更难模仿的能力组合。' },
              { title: '尚未兑现的优势', detail: '资源满足价值、稀缺与难模仿检验，但组织尚未准备好利用。补齐治理、流程、人才、预算与激励。' },
              { title: '持续竞争优势', detail: '四项检验全部通过。保护关键机制、持续再投资并监测竞争环境，避免优势因外部变化而自然衰减。' },
            ],
          },
          useCases: [
            '盘点企业、业务单元或团队的核心资源与能力',
            '评估一项能力是否值得继续投资、保护或扩大应用',
            '比较竞争优势并支持战略、产品或市场选择',
            '识别关键人才能力与组织机制之间的承接缺口',
          ],
          pitfalls: [
            { title: '把擅长直接等同于价值', detail: '内部认可不代表战略价值。必须说明资源如何创造客户或业务收益、把握机会或降低威胁，并比较其投入成本与替代方案。' },
            { title: '跳过顺序或只挑有利答案', detail: 'VRIO 是逐级检验。资源如果没有价值，继续讨论稀缺和难模仿没有意义；每一项结论都应有可以复核的证据。' },
            { title: '把当前稀缺当作永久稀缺', detail: '竞争者会学习、购买、合作或寻找替代方案。分析时应考虑资源能否持续获得，以及市场、技术和人才流动会怎样改变稀缺性。' },
            { title: '只看单项资源', detail: '真正难以复制的优势往往来自多项能力、关系和流程的组合。只评估单一技能，可能低估组织协同形成的独特性。' },
            { title: '忽略组织承接条件', detail: '没有清晰职责、流程、预算、激励和使用场景，再有价值且稀缺的资源也无法稳定创造结果。' },
          ],
          managerApplication: [
            '在团队或员工发展讨论中，先选择与业务目标最相关的三到五项能力，再用实际成果、客户反馈、同事观察和外部比较逐项回答 VRIO 问题。不要把个人偏好或一次成功经历直接当作稳定优势。',
            '区分“员工不具备能力”和“组织没有让能力发挥作用”。如果前三项条件成立而结果仍不明显，优先检查岗位配置、授权、流程、工具、协作关系和激励，而不是简单要求员工投入更多努力。',
            '将结论连接到真实任务：对持续优势安排保护、传承和更高价值的应用；对暂时优势加快转化；对基础能力控制成本并保持标准；对缺乏价值的投入及时调整。',
          ],
          keyTakeaway: '资源只有同时具备价值、稀缺性和难模仿性，并被组织真正承接和利用，才可能形成持续竞争优势。',
        },
      },
      {
        id: 'mckinsey-7s',
        path: toolPath('strategy-analysis', 'mckinsey-7s'),
        title: 'McKinsey 7S Framework',
        summary: '从战略、结构、系统、共同价值观、技能、领导风格和人员七个要素诊断组织是否真正对齐。',
        summaryEnglish: 'Diagnose organizational alignment across strategy, structure, systems, shared values, skills, style, and staff.',
        application: '适合战略转型、并购整合、组织重组和执行效果不佳时定位系统性错位。',
        icon: 'alignment',
        detail: {
          subtitle: '用七个相互依赖的组织要素诊断执行断点，并把战略、文化、能力和日常工作机制重新拉回同一方向。',
          overviewTitle: '从结构优先到系统协同：什么是 McKinsey 7S？',
          overview: [
            'McKinsey 7S 形成于 20 世纪 70 年代后期。Tom Peters、Robert Waterman 与 Richard Pascale、Anthony Athos 等研究者共同挑战了当时“只要组织图正确，绩效自然会改善”的结构优先思路。该模型在 1980 年前后进入管理实践，并把管理者的注意力从单一结构调整扩展到完整组织系统。',
            '框架把 Strategy、Structure 和 Systems 归为较容易界定、记录和直接调整的硬要素；Shared Values、Skills、Style 和 Staff 则是更难量化、变化较慢却深刻影响行为的软要素。Shared Values 位于中心，因为真实的信念、文化规范和奖励信号会影响其余六项如何运作。',
            '七项之间不存在简单的先后层级。新战略会提出新技能要求，新技能需要人员机制和领导方式支持，流程与绩效系统也必须同步改变。7S 的重点不是让每一项单独获得高分，而是验证七项是否共同支持同一方向。',
          ],
          frameworkTitle: '七个要素如何共同决定组织执行力',
          framework: [
            { title: 'Strategy · 战略', detail: '组织选择在哪里竞争、如何形成优势，以及如何向关键优先事项配置资源。诊断时检查：战略是否被明确表达并被各层级理解？预算和管理注意力是否匹配重点？相对于竞争者是否具有清晰取舍？' },
            { title: 'Structure · 结构', detail: '组织层级、汇报关系、职责分工、决策权和跨部门协调方式。需要判断结构是在支持战略还是制造摩擦，角色边界是否清楚，以及信息和工作能否顺畅跨越职能与业务单元。' },
            { title: 'Systems · 系统', detail: '推动日常运营的正式流程、制度和工作流，包括规划、预算、绩效管理、信息系统和业务流程。关键问题是这些机制正在强化新战略，还是仍然奖励旧目标并拖慢执行。' },
            { title: 'Shared Values · 共同价值观', detail: '组织成员真正相信什么，以及哪些文化规范决定日常行为。应比较公开价值主张与实际决策、奖励和领导示范是否一致，并检查不同团队是否遵循相容的工作原则。' },
            { title: 'Skills · 技能', detail: '组织作为整体具备的独特能力，而不仅是少数个人的专长。需要识别当前优势、相对于未来战略的关键能力缺口，以及招聘、实践、学习和知识传承能否持续建立新能力。' },
            { title: 'Style · 领导风格', detail: '领导者实际上如何使用时间、设定重点、作出决定、授权并奖励行为。观察真实管理行为传递出的信号，并判断决策方式、风险偏好与授权程度是否符合战略需要。' },
            { title: 'Staff · 人员', detail: '人员组合、关键岗位和人才管道，以及组织吸引、配置、发展与保留人才的方式。应检查未来需要什么人才、关键人员为何流失，以及继任和发展项目是否在建立正确能力。' },
          ],
          processTitle: '如何用 7S 规划组织变革：四个步骤',
          process: [
            { title: '绘制真实的当前状态', detail: '基于绩效数据、员工反馈和可观察行为描述七项现状，避免依据制度文本或管理层愿望作答。可以按 1 至 5 分评估每项，并逐对标记对齐、部分对齐或冲突。' },
            { title: '定义可观察的目标状态', detail: '把战略目标转化为七项具体结果。不要只写“更敏捷”或“加强协同”，而要说明需要什么能力、何种决策方式、哪些流程变化、由谁建立以及何时能够验证。' },
            { title: '识别差距与依赖关系', detail: '比较当前状态和目标状态，找出差距最大的要素以及它们之间的连接。例如，建立新技能同时需要招聘与发展机制、管理者授权和支持试验的绩效系统。' },
            { title: '按依赖关系安排变化顺序', detail: '先处理 Shared Values 等会影响多项行为的基础错位，再推进差距最大的要素。每项行动都要明确负责人、期限和验证信号，并持续观察它对其他六项造成的连锁影响。' },
          ],
          useCases: [
            '战略发生变化但组织执行和结果没有同步改善时',
            '并购后需要诊断文化、流程、人才和管理方式的整合问题时',
            '组织重组、运营模式调整或跨部门协作长期受阻时',
            '转型项目只改变制度和结构，却没有形成行为变化时',
          ],
          pitfalls: [
            { title: '只关注硬要素', detail: '战略、结构和系统更容易被写进方案，但共同价值观、能力、领导风格和人员往往决定新设计能否真正运行。只改变组织图，很少能够自动改变实际工作体验。' },
            { title: '把 7S 当作一次性审计', detail: '组织会持续变化，任何一项调整也会重新改变其他六项之间的关系。应在季度战略复盘、转型里程碑或重大变化后重新检查对齐状态。' },
            { title: '忽略 Shared Values 的中心作用', detail: '如果真实文化仍在奖励旧行为，新战略和新结构会被旧工作方式抵消。共同价值观必须通过领导示范、决策取舍和奖励信号来判断。' },
            { title: '把目标状态当成当前事实', detail: '团队容易依据制度文本和领导意图评分。当前状态必须由实际行为、流程数据和员工反馈证明，并与目标状态分开记录。' },
            { title: '只给单项评分，不检查相互关系', detail: '单项强弱只是分析的一半。真正洞察来自成对检查，例如绩效系统是否支持新战略、人员机制能否形成所需技能、领导方式是否符合授权要求。' },
          ],
          managerApplication: [
            '当团队在战略调整、重组或新流程落地后表现不佳时，经理应先用七个要素收集事实，而不是直接归因于员工态度。比较管理层设计与一线实际体验，可以识别正式机制和真实工作方式之间的落差。',
            '在员工沟通中区分个人能力问题与组织条件问题：目标是否清晰、决策权是否匹配、流程是否支持、领导行为是否一致、人员配置是否合理。这样可以避免让个人承担本应由系统解决的障碍。',
            '行动计划应同时说明要调整的要素及其依赖关系，并优先处理会影响多个要素的断点。复盘时不仅检查单项是否改善，还要确认七项是否更一致、战略执行是否出现可观察变化。',
          ],
          keyTakeaway: 'McKinsey 7S 的核心判断是：组织一致性比孤立优化更重要。只有七个相互依赖的要素共同支持同一方向，战略才可能转化为稳定执行。',
        },
      },
      {
        id: 'skills-gap-analysis',
        path: toolPath('strategy-analysis', 'skills-gap-analysis'),
        title: 'Skills Gap Analysis',
        summary: '比较员工当前能力与业务现在及未来所需能力，识别关键差距并选择最合适的补足方式。',
        summaryEnglish: 'Compare current employee capabilities with present and future business needs, then identify priority gaps and the right response.',
        application: '适合战略变化、新项目或技术落地、岗位重设计、人才规划和学习投资决策。',
        icon: 'skills',
        detail: {
          subtitle: '从业务任务出发，用一致证据识别个人、团队和组织层面的能力差距，再把优先差距转化为人才行动。',
          overviewTitle: '什么是技能差距分析？',
          overview: [
            '技能差距分析用于评估员工当前具备的能力，与业务战略在当前或未来要求的能力之间存在多大差距。它既包括分析、项目管理和数字技术等硬技能，也包括批判性思考、解决问题、管理、沟通与协作等软技能。',
            '分析可以在个人、团队或组织三个层级开展：岗位职责变化或个人表现不达标时关注个人；新项目、新任务或新技术落地时关注团队；战略调整、业务目标未达成或未来能力不足时关注整个组织。',
            '技能差距分析不是单纯制作“缺少哪些技能”的清单。它应帮助组织看清现有人才能力、确定学习与发展重点、支持战略性人员规划和招聘，并在培养、岗位重设计、招聘或临时人才之间选择更有效的干预方式。',
          ],
          frameworkTitle: '技能差距分析的四类核心信息',
          framework: [
            { title: '分析范围与业务目标', detail: '明确分析对象是个人、团队还是整个组织，以及它要支持的任务、项目、战略变化和时间范围。先回答业务使命与目标是什么，再决定哪些能力值得评估，避免从现有课程目录反推需求。' },
            { title: '关键能力与目标水平', detail: '同时定义现在和未来所需的硬技能、软技能及目标熟练度。能力应具体到岗位和任务；如果缺少某项能力会导致任务结果不合格，它应被视为关键能力，而不是可有可无的要求。' },
            { title: '当前能力与可靠证据', detail: '通过岗位资料、绩效结果、能力测评、实际任务、自评、经理观察、访谈、焦点小组或 360 度反馈盘点现状。将结果集中记录，并让评估者使用同一套行为标准和评分尺度。' },
            { title: '差距权重与干预选择', detail: '比较当前水平和目标水平，同时考虑能力的业务关键性、使用频率、未来趋势、差距大小和补足时限。优先处理经常使用且直接影响战略结果的差距，再决定培养、岗位调整、招聘或其他方案。' },
          ],
          processTitle: '如何完成技能差距分析：三个阶段',
          process: [
            { title: '范围界定与诊断', detail: '确定个人、团队或组织层级，连接使命、业务目标和未来工作。识别当前及未来所需能力时，同时考虑自动化、行业趋势、监管变化、新岗位和扩展任务，并区分关键能力与非关键能力。' },
            { title: '数据收集与分析', detail: '更新岗位画像和能力标准，收集绩效评价、能力测评、访谈、自评、经理及员工反馈等多源数据。使用统一的 1 至 5 分或行为等级分别记录当前水平、目标水平和使用频率，再交叉比较并排列差距优先级。' },
            { title: '设计干预并持续复盘', detail: '根据差距性质组合使用在岗实践、培训、教练、再培训、岗位重设计、内部流动、继任规划、外部招聘或临时人才。明确负责人、预算、时间和成效指标，取得员工与管理层支持，并根据业务变化定期重新分析。' },
          ],
          useCases: [
            '战略调整、新项目、自动化或新技术即将改变工作任务时',
            '员工岗位职责变化、晋升准备或表现未达到明确标准时',
            '团队需要承担不同任务、建立新能力或改善项目交付时',
            '开展人员规划、继任、招聘、岗位重设计或学习预算配置时',
          ],
          pitfalls: [
            { title: '只评估技术技能', detail: '数字和专业技能更容易量化，但沟通、适应性、领导和问题解决等软技能也可能决定任务能否成功。评估范围应由业务结果决定，而不是由测量便利性决定。' },
            { title: '只看当前岗位，不看未来变化', detail: '照搬现有岗位说明会得到短期答案。应把自动化、行业趋势、监管要求和战略变化纳入目标能力，否则方案可能在完成前已经过时。' },
            { title: '评分标准不一致或只依赖自评', detail: '不同评估者对熟练度理解不同，自评也可能高估或低估能力。为每个等级定义可观察行为，并用绩效、任务、经理和同事反馈交叉验证。' },
            { title: '把所有差距都交给培训', detail: '课程并不适合所有问题。有些差距需要真实任务、教练或再培训，有些来自岗位设计和流程，还有些需要招聘、内部流动或临时人才。' },
            { title: '缺少参与、支持和持续更新', detail: '员工未被纳入分析容易抵触变化，管理层不支持则难以获得资源。技能差距分析也不应是一次性项目，应在战略、技术和岗位发生变化时定期复查。' },
          ],
          managerApplication: [
            '与员工讨论能力差距前，先解释业务目标、岗位任务和目标熟练度，并邀请员工补充自评、已有经验和未来意愿。把讨论聚焦在具体行为与任务结果，避免将“差距”表达成人格评价。',
            '结合实际成果、工作样本、经理观察和他人反馈判断当前水平，同时区分员工尚未具备能力、没有机会展示能力，以及流程或资源阻碍表现这三种不同情况。',
            '为少量高优先级差距选择匹配的行动和真实应用场景，明确支持人、复盘节点和衡量信号。复盘时检查能力是否提升、是否被工作持续使用，以及干预是否改善了业务结果。',
          ],
          keyTakeaway: '有效的技能差距分析必须把业务未来需要、当前能力证据和合适的人才干预连接起来；识别差距只是起点，关闭关键差距才是结果。',
        },
      },
    ],
    workflow: [
      { title: '界定问题', detail: '先明确决策对象、时间范围和成功标准。' },
      { title: '交叉验证', detail: '结合多种框架和事实来源，避免单一视角。' },
      { title: '转化行动', detail: '把结论写成责任人、节点和验证信号。' },
    ],
    ctaTitle: '把分析结论带入真实沟通',
    ctaDescription: '在沟通工作台中整理员工背景、选择意图，并将分析转化为可执行的经理对话。',
    accent: '#006b73',
    accentSoft: '#e7f4f2',
  },
  'coaching-goals': {
    slug: 'coaching-goals',
    path: '/solutions/coaching-goals',
    navLabel: '教练与目标',
    navLabelEnglish: 'Coaching & Goal',
    title: '教练与目标：推动真实进展的框架',
    titleEnglish: 'Coaching and goals: the frameworks behind real progress',
    summary: '为目标设定和教练对话建立清晰节奏，让思考、选择与责任真正连接起来。',
    summaryEnglish: 'Create a practical rhythm for goals and coaching so reflection, choice, and accountability stay connected.',
    heroImage: `${LANDING_ASSET}/solution-category-coaching-premium-202609-v2.webp`,
    heroImageAlt: '两位专业人士在明亮办公室进行一对一辅导交流',
    introTitle: '让目标从愿望变成可以跟进的承诺',
    introParagraphs: [
      '没有结构的目标容易停留在愿望，没有结构的教练对话则容易变成漫无方向的交流。可重复的框架帮助经理保持聚焦，同时给员工充分的思考空间。',
      'GROW 提供对话路径，SMART 帮助定义结果，职业教练方法把个人诉求、发展机会和现实约束连接起来。框架的价值在于形成持续行动，而不是完成一次表格。',
    ],
    toolsHeading: '教练与目标',
    toolsHeadingEnglish: 'Coaching and goals',
    tools: [
      {
        id: 'grow-model',
        path: toolPath('coaching-goals', 'grow-model'),
        title: 'GROW Model',
        summary: '沿着目标、现实、选择和行动承诺四个阶段，引导员工从清晰结果走向自主决策与可跟踪行动。',
        summaryEnglish: 'Guide employees through goal, reality, options, and commitment so clear outcomes lead to autonomous decisions and trackable action.',
        application: '适合一对一教练、绩效评估、职业发展、团队教练、问题解决和自我反思。',
        icon: 'coaching',
        detail: {
          subtitle: '用一条可以往返的思考路径，帮助员工看清目标与现状之间的差距，扩展选择并对下一步负责。',
          overviewTitle: '什么是 GROW 模型？',
          overview: [
            'GROW 是广泛用于领导力、绩效发展和组织教练的对话与问题解决框架。四个阶段分别是 Goal（目标）、Reality（现实）、Options（选择）和 Will / Way Forward（意愿与前进路径），帮助个人或团队从“想实现什么”逐步走到“具体会做什么”。',
            '它遵循一种自然的思考过程：先明确期望结果，再诚实理解当前情况，随后拓展可能路径，最后作出行动承诺。四个阶段并非必须一次直线走完；当现实信息改变了目标，或选项暴露出新的限制时，对话可以回到前一阶段重新澄清。',
            'GROW 的效果不只来自四个字母。教练需要使用开放问题、专注倾听、复述、总结和非评判式探索，让员工形成自己的洞察与选择。这样既能看清目标与现实之间的差距，也能提升思路广度、行动责任和持续跟进的质量。',
          ],
          frameworkTitle: 'GROW 的四个阶段',
          framework: [
            { title: 'Goal · 目标', detail: '明确本次对话、当前任务或更长期发展真正要实现的结果。有效目标应清楚、具体、对员工本人有意义，在现实期限内可达成，并与更大的职业或组织方向一致。可以询问：希望这次对话结束时更清楚什么？成功会呈现哪些变化？为什么现在值得处理？' },
            { title: 'Reality · 现实', detail: '用事实和证据理解当前起点，包括现有行为与结果、已经取得的进展、正在发挥作用的优势和资源，以及障碍、约束和未验证假设。可以询问：目前实际发生了什么？已经尝试过什么？哪些证据支持你的判断？还有什么假设需要检查？' },
            { title: 'Options · 选择', detail: '在给建议或作判断之前，先帮助员工产生多种可能路径，并比较每种选择的价值、成本、风险和所需支持。目标是拓宽思考，而不是让员工猜经理心中的答案。可以询问：还可能怎么做？如果暂时不受限制会尝试什么？谁或什么资源可以提供帮助？' },
            { title: 'Will / Way Forward · 意愿与前进路径', detail: '把洞察转化为真正愿意承担的行动，明确第一步、完成时间、进度证据、所需支持和问责方式。可以用 1 至 10 分检查承诺度，并提前讨论可能出现的阻碍及应对办法，确保选择不会停留在讨论中。' },
          ],
          processTitle: '如何开展一次 GROW 教练对话：六个实践步骤',
          process: [
            { title: '建立对话边界与聚焦点', detail: '确认可用时间、对话目的和双方角色，说明哪些内容由员工自主探索，哪些属于不可协商的业务要求。邀请员工定义当前最需要处理的目标、挑战或决定，让对话从真实需求开始。' },
            { title: '把期望转化为清晰目标', detail: '帮助员工描述想要的结果、成功信号、目标的重要性以及与更大方向的关系。目标还不清楚时不要急着讨论方案；先确认双方对“这次对话取得进展”有相同理解。' },
            { title: '用证据看清当前现实', detail: '探索现有行为、结果、进展、优势、资源、障碍和假设，区分可观察事实与个人解释。通过复述和总结检查理解，并把 Goal 与 Reality 并列比较，明确真正需要跨越的差距。' },
            { title: '先拓展选择，再评价路径', detail: '鼓励员工在不被过早评判的情况下提出多个想法，包括尚未尝试的方式、可借助的人和资源。形成足够选项后，再比较影响、利弊、风险、现实约束和个人意愿。' },
            { title: '把选择变成行动承诺', detail: '由员工选择准备执行的方案，明确第一步、开始时间、里程碑、成功证据和支持需求。检查承诺度以及可能阻碍行动的因素；承诺不足时应继续调整行动，而不是勉强结束对话。' },
            { title: '建立问责并循环复盘', detail: '约定谁会跟进、何时检查以及如何记录进展。复盘时以行动结果作为新的 Reality，判断目标、假设或方案是否需要更新，再进入下一轮选择与承诺。' },
          ],
          useCases: [
            '一对一教练或日常管理对话需要形成清晰行动时',
            '绩效评估需要从结果差距走向改进行动时',
            '员工需要探索职业方向、能力发展或重要决定时',
            '团队需要共同定义目标、现实、选项与责任时',
            '复杂问题需要拓宽思路而不是立即接受单一答案时',
            '个人希望用结构化问题开展自我反思和自我教练时',
          ],
          pitfalls: [
            { title: '过早跳到解决方案', detail: '目标和现实尚未澄清时，最快出现的方案往往只处理表面症状。先确认真正结果、事实和差距，再进入选择阶段。' },
            { title: '把 GROW 当成固定顺序的问卷', detail: '机械地依次问完四组问题会破坏真实对话。新的事实或洞察出现时，应自然返回目标或现实阶段重新校准。' },
            { title: '用问题引导员工接受经理的答案', detail: '诱导式问题只是换一种方式下指令。应提出开放问题、允许思考和沉默，并在确实需要建议时明确切换角色，而不是伪装成员工自己的发现。' },
            { title: '目标宽泛或与员工无关', detail: '模糊目标无法指导选项和行动，缺少个人意义的目标也难以形成承诺。需要明确成功表现、现实期限和目标为什么值得员工投入。' },
            { title: '有行动清单却没有问责', detail: '没有时间、进度证据和跟进安排的行动很容易消失在日常工作中。明确下一步、检查节点、支持关系和障碍应对，并在复盘时形成新的 Reality。' },
          ],
          managerApplication: [
            '经理应把主要注意力放在开放提问、倾听、复述和总结上，让员工解释自己的目标、证据、假设和选择。一个好的教练问题应打开思考，而不是暗示唯一正确答案。',
            '教练方式不等于放弃管理责任。涉及绩效标准、合规要求或不可协商的结果时，经理应先清楚说明边界，再使用 GROW 帮助员工探索如何达到要求，并明确哪些决定仍由经理承担。',
            '在 Will / Way Forward 阶段检查行动是否具体、员工是否真正愿意承担、需要什么支持以及怎样跟进。后续复盘应重新查看结果和证据，让 GROW 成为持续改善循环，而不是一次性谈话模板。',
          ],
          keyTakeaway: 'GROW 的核心不是按顺序问完四组问题，而是用开放提问和倾听帮助员工澄清目标、看见现实、拓展选择，并对可跟踪的下一步作出自己的承诺。',
        },
      },
      {
        id: 'smart-goals',
        path: toolPath('coaching-goals', 'smart-goals'),
        title: 'SMART Goals',
        summary: '用具体、可衡量、可实现、相关和有时限五项标准，把模糊目标转化为可跟踪的实际工作。',
        summaryEnglish: 'Use specific, measurable, achievable, relevant, and time-bound criteria to turn vague goals into trackable work.',
        application: '适合业务目标、团队项目、绩效与发展目标，以及需要明确成功标准和期限的行动计划。',
        icon: 'goal',
        detail: {
          subtitle: '在开始执行前定义结果、衡量方式、现实条件和时间边界，并在执行过程中持续使用目标指导决策。',
          overviewTitle: '什么是 SMART 目标？',
          overview: [
            'SMART 是一种目标设定框架，由 Specific（具体）、Measurable（可衡量）、Achievable（可实现）、Relevant（相关）和 Time-bound（有时限）五项标准组成。它把“提升业绩”或“改善能力”等方向性愿望转化为能够计划、执行和判断是否完成的目标。',
            '高质量 SMART 目标会在工作开始前明确成功是什么、用什么证据衡量、团队是否具备必要能力和资源、目标为什么值得投入，以及何时完成。这样能够减少团队对结果的不同理解，并为优先级、资源配置和进度判断建立共同依据。',
            'SMART 不是写完一句目标陈述就结束。目标需要被相关人员看见，与项目和日常任务连接，按固定节奏检查里程碑，并在完成后复盘结果。对于复杂、长期或探索性工作，可以先拆分阶段目标，并用愿景、战略或 OKR 补充 SMART 的短期执行视角。',
          ],
          frameworkTitle: 'SMART 的五项标准',
          framework: [
            { title: 'Specific · 具体', detail: '聚焦一个清晰、界定明确的结果，而不是宽泛意愿。说明要改变什么、涉及谁、属于哪个项目或业务结果，以及成功时会呈现什么。目标越具体，团队越容易判断哪些任务相关、哪些工作应被排除。' },
            { title: 'Measurable · 可衡量', detail: '加入能够客观判断进展和完成的数据、比例、数量、交付物、质量标准或行为证据。优先明确当前基线、目标值和数据来源；对于重复工作，可以使用历史基准帮助设定合理且可比较的指标。' },
            { title: 'Achievable · 可实现', detail: '目标应具有挑战，但不能完全超出可用技能、资源和控制范围。检查团队是否具备所需能力、预算、工具、决策权限和支持；如果条件不足，应调整范围、期限或先补齐依赖，而不是保留无法兑现的目标。' },
            { title: 'Relevant · 相关', detail: '确认目标与更大的团队重点、业务成果或个人发展方向直接相关，并值得投入当前资源。还要校验现实约束：技术上可以完成，并不代表要求所有人长期加班的方案就是合理选择。' },
            { title: 'Time-bound · 有时限', detail: '为目标设定明确结束日期，并为较大目标安排阶段里程碑和检查节奏。时间边界能够建立紧迫感、限制范围蔓延，也让团队知道何时应评估结果、调整计划或停止投入。' },
          ],
          processTitle: '如何写出并落实 SMART 目标：六个步骤',
          process: [
            { title: '定义单一且具体的结果', detail: '从一个需要产生的变化开始，将宽泛方向缩小到特定项目、对象或成果。先写“要实现什么结果”，再判断活动是否真正服务于该结果，避免把完成任务误当成取得成效。' },
            { title: '确定成功的衡量方式', detail: '为目标加入数字、比例、质量标准、交付物或可观察行为，并记录基线、目标值和数据来源。确保团队在开始前就能回答“怎样知道目标已经完成”。' },
            { title: '验证目标是否可以实现', detail: '检查人员能力、工作量、预算、工具、依赖关系和员工能够控制的范围。目标不能过于轻松，但也不应建立在未确认的资源或持续超负荷工作之上。' },
            { title: '校验相关性与现实条件', detail: '说明目标如何支持团队或组织的更高层目标，并比较投入与预期价值。邀请会影响或依赖结果的相关方共同检查隐藏约束、优先级冲突和不现实假设。' },
            { title: '设定期限与阶段节点', detail: '明确最终截止日期，并把较大目标拆成可检查的里程碑。为每个阶段指定负责人和预期证据，使团队能够在偏差仍然较小时发现问题。' },
            { title: '连接日常工作并持续复盘', detail: '在项目开始时向团队和相关方公开目标，将项目、任务和负责人连接到目标，按固定节奏检查进展。结束后依据预先定义的标准评估结果，并把规划、执行和假设中的经验用于下一轮目标。' },
          ],
          useCases: [
            '将组织或业务方向转化为可判断的阶段成果时',
            '团队需要对项目结果、负责人、指标和期限形成共同理解时',
            '经理与员工设定绩效、能力发展或行为改变目标时',
            '个人需要把职业或学习意愿转化为可持续行动时',
          ],
          pitfalls: [
            { title: '把活动当成结果', detail: '“完成培训”或“举办会议”只是活动。继续追问活动需要带来什么能力、行为、客户体验或业务变化，并用该变化定义成功。' },
            { title: '选择容易统计但没有意义的指标', detail: '可量化不等于有价值。指标必须能够代表真正期望的结果，并有可靠基线和数据来源，否则团队可能优化数字却没有改善实际表现。' },
            { title: '忽略资源与控制范围', detail: '目标理论上可达成，不代表在现有人员、预算和时间下现实。明确员工能够影响的部分、外部依赖及组织必须提供的支持，避免把系统条件变成员工责任。' },
            { title: '用 SMART 过度简化复杂或长期工作', detail: '复杂工作可能包含多个相互依赖的结果，长期创新也需要探索空间。可以拆分阶段目标，并用愿景或战略框架保持长期方向，避免过度关注短期数字和单一目标。' },
            { title: '设定后不再使用目标', detail: '目标如果只留在文档里，就无法指导优先级。应公开目标、连接日常任务、定期查看里程碑，并根据实际变化透明调整路径或假设。' },
          ],
          managerApplication: [
            '经理应与员工及相关方共同校验 SMART 目标，而不是单方面套用五个字母。先对齐需要改变的业务或行为结果，再讨论指标、资源、控制范围和时间，让隐藏约束在执行前被看见。',
            '目标确定后，应让员工知道个人工作如何连接团队与组织目标，并把目标关联到真实项目、任务和负责人。共享目标能够减少优先级冲突，也便于相关方及时提供支持。',
            '跟进时同时查看最终指标、阶段里程碑、风险和环境变化，不要等到截止日才判断成败。完成后依据原定标准复盘结果和原因，将未达成视为需要分析的规划或执行证据，而不是简单的人格评价。',
          ],
          keyTakeaway: 'SMART 的核心是在执行前定义清楚成功，并在执行中持续连接目标与工作；具体、可衡量的目标只有被共享、跟踪和复盘，才会真正产生结果。',
        },
      },
      {
        id: 'career-coaching-tools',
        path: toolPath('coaching-goals', 'career-coaching-tools'),
        title: 'Career Coaching',
        summary: '连接职业诉求、优势、发展机会和现实路径，形成可持续的成长选择。',
        summaryEnglish: 'Connect career aspirations, strengths, development opportunities, and realistic paths to make sustainable growth choices.',
        application: '适合发展对话、岗位转换、高潜培养和职业停滞讨论。',
        icon: 'development',
        detail: {
          subtitle: '把职业愿望、当前能力、现实机会和具体行动放进同一场有结构的对话。',
          overviewTitle: '什么是职业教练工具？',
          overview: [
            '职业教练工具是一组帮助员工理解方向、评估现实、探索路径并建立行动责任的框架。常见组合包括 GROW、SMART、技能差距分析、SWOT 和反思实践。',
            '这些工具提供结构但不替员工做决定。高质量职业对话应让员工更清楚自己在哪里、想去哪里，以及愿意采取什么行动。',
          ],
          frameworkTitle: '职业教练的四个关注面',
          framework: [
            { title: '方向与意义', detail: '理解员工希望获得的成长、贡献、工作方式和长期价值。' },
            { title: '能力与证据', detail: '识别优势、可迁移能力、当前差距和实际成果。' },
            { title: '机会与约束', detail: '探索岗位、任务、网络、组织需求以及现实限制。' },
            { title: '行动与反思', detail: '将方向转化为实践、反馈、学习和定期复盘。' },
          ],
          processTitle: '如何组织职业教练对话',
          process: [
            { title: '理解真正诉求', detail: '区分职位名称背后的成长、影响、认可、稳定或自主需求。' },
            { title: '建立现实画像', detail: '结合成果、反馈、能力和当前机会校验员工的自我判断。' },
            { title: '扩展多条路径', detail: '同时考虑岗位变化、任务拓展、横向经历、专业深化和关系网络。' },
            { title: '形成发展实验', detail: '选择风险可控的真实任务验证兴趣、能力和匹配度。' },
            { title: '设置复盘节奏', detail: '定期比较新证据与原有假设，必要时调整方向。' },
          ],
          useCases: ['年度发展对话', '职业停滞或转型', '高潜人才发展', '晋升前准备'],
          pitfalls: [
            { title: '把职业对话等同晋升承诺', detail: '讨论发展可能性时必须清楚区分探索、准备和正式机会。' },
            { title: '只讨论职位名称', detail: '需要继续探索职位背后的动机、能力和工作体验。' },
            { title: '经理替员工规划', detail: '经理提供信息和挑战，职业选择仍应由员工承担。' },
          ],
          managerApplication: [
            '经理应透明说明组织机会和限制，同时帮助员工看见当前岗位内也可以获得的发展经历。',
            '不要把一次对话当作最终答案。职业判断需要通过真实任务、反馈和持续反思逐步形成。',
          ],
          keyTakeaway: '职业教练的成果不是一张理想职位清单，而是一组可以验证方向的发展行动。',
        },
      },
    ],
    workflow: [
      { title: '多问少讲', detail: '用开放问题帮助员工形成自己的判断。' },
      { title: '落到证据', detail: '让目标和进展都有可观察的判断依据。' },
      { title: '持续复盘', detail: '把跟进作为教练过程的一部分。' },
    ],
    ctaTitle: '先练习，再进入真实教练对话',
    ctaDescription: '通过谈前指导和多轮预演，检验问题顺序、目标清晰度与员工可能的回应。',
    accent: '#2f6f44',
    accentSoft: '#eaf4ed',
  },
  'feedback-assessment': {
    slug: 'feedback-assessment',
    path: '/solutions/feedback-assessment',
    navLabel: '反馈与评估',
    navLabelEnglish: 'Feedback & Assessment',
    title: '反馈与评估：建立自我认知，打造更好的团队',
    titleEnglish: 'Feedback and assessment: build self-awareness, build better teams',
    summary: '用结构化反馈和评估建立可靠认知，让发展讨论从印象走向可验证的证据。',
    summaryEnglish: 'Build reliable insight through structured feedback and assessment grounded in observable evidence.',
    heroImage: `${LANDING_ASSET}/solution-category-feedback-premium-202609.webp`,
    heroImageAlt: '玻璃倒影中的专业人员在开放办公空间协作',
    introTitle: '先看清能力，再决定如何发展',
    introParagraphs: [
      '自我认知、他人体验和实际工作证据经常存在差异。结构化评估能够把这些视角放在一起，让反馈更完整，也让发展投入更聚焦。',
      '360 度反馈观察行为影响，自我评估促进反思，团队角色理解协作贡献，软技能与岗位技能评估则聚焦具体能力。不同方法共同遵循一个原则：发展必须建立在可靠证据上。',
    ],
    toolsHeading: '反馈与评估',
    toolsHeadingEnglish: 'Feedback and assessment',
    tools: [
      {
        id: '360-degree-feedback',
        path: toolPath('feedback-assessment', '360-degree-feedback'),
        title: '360-Degree Feedback',
        summary: '汇集上级、同级、下属和合作方视角，理解行为被他人如何体验。',
        summaryEnglish: 'Combine perspectives from managers, peers, direct reports, and partners to understand how others experience a person\'s behavior.',
        application: '适合领导力发展、关键岗位培养和行为改变项目。',
        icon: 'feedback',
        detail: {
          subtitle: '将自我认知与多方真实体验放在一起，发现单一视角难以看见的行为模式。',
          overviewTitle: '什么是 360 度反馈？',
          overview: [
            '360 度反馈是一种多来源反馈过程，通常包括本人、上级、同事、下属以及重要合作方。反馈一般保持匿名，并围绕明确的行为能力收集。',
            '最有价值的信息来自模式与差异：不同群体反复观察到什么，自评与他评在哪里一致或不同。它更适合发展，而不是直接用于薪酬或晋升决策。',
          ],
          frameworkTitle: '高质量 360 的关键组成',
          framework: [
            { title: '清晰目的', detail: '说明评估用于发展什么，以及结果将如何被使用和保护。' },
            { title: '代表性评价者', detail: '选择经常观察相关行为、能够提供具体反馈的人。' },
            { title: '行为化题项', detail: '围绕可观察行为设计量表和开放问题，避免抽象人格标签。' },
            { title: '模式分析', detail: '比较群体趋势、自他差异和反复出现的主题，而不是追查单条评价。' },
            { title: '专业反馈与行动', detail: '通过结构化反馈会谈理解结果，并形成少量发展重点。' },
          ],
          processTitle: '如何实施 360 度反馈',
          process: [
            { title: '定义能力与用途', detail: '选择与角色和发展目标真正相关的行为维度。' },
            { title: '选择评价者', detail: '覆盖不同协作关系，并确保每组人数能够保护匿名。' },
            { title: '收集反馈', detail: '说明保密规则，鼓励评价者提供具体、建设性的观察。' },
            { title: '分析结果', detail: '识别一致信号、显著差异和最重要的两到三个发展主题。' },
            { title: '完成反馈会谈', detail: '承接情绪、澄清含义，并把洞察转化为行为实验和跟进。' },
          ],
          useCases: ['领导力发展项目', '新角色转换', '关键人才培养', '行为改变跟踪'],
          pitfalls: [
            { title: '与薪酬晋升绑定', detail: '评价者会降低坦诚度，发展价值也会明显下降。' },
            { title: '交付报告后结束', detail: '没有反馈会谈和行动跟进，数据很难产生行为改变。' },
            { title: '追查评价者', detail: '关注模式而不是猜测谁说了什么，否则会破坏信任。' },
          ],
          managerApplication: [
            '经理应帮助员工理解模式和影响，不急于为每项低分辩护或解释。',
            '选择一到两个对业务和团队体验最关键的行为，并约定员工可以获取持续反馈的方式。',
          ],
          keyTakeaway: '360 度反馈的价值来自多源模式、自他差异和后续行动，而不是一张分数排名。',
        },
      },
      {
        id: 'leadership-self-assessment',
        path: toolPath('feedback-assessment', 'leadership-self-assessment'),
        title: 'Leadership Self-Assessment',
        summary: '以明确标准回顾自己的领导行为、能力证据和发展重点。',
        summaryEnglish: 'Review leadership behavior, capability evidence, and development priorities against clear criteria.',
        application: '适合发展计划启动、角色转换和阶段性复盘。',
        icon: 'leadership',
        detail: {
          subtitle: '用统一行为标准和真实证据完成领导力反思，并主动寻找外部验证。',
          overviewTitle: '什么是领导力自我评估？',
          overview: [
            '领导力自我评估是个人依据明确能力框架，对自己的领导行为和证据进行结构化判断。它快速、易用，但会受到自我认知偏差影响。',
            '因此自评适合作为发展起点，而不是最终结论。高质量自评会结合实际案例、绩效数据和他人反馈，并最终形成具体发展行动。',
          ],
          frameworkTitle: '有效自评的五项设计',
          framework: [
            { title: '能力框架', detail: '选择覆盖当前角色关键要求的行为维度。' },
            { title: '一致量表', detail: '为不同等级提供清楚的行为描述，降低主观解释差异。' },
            { title: '事实证据', detail: '每个判断都应尽量关联具体情境、行为和结果。' },
            { title: '开放反思', detail: '补充最自豪的表现、反复出现的困难和可能的盲点。' },
            { title: '外部校验', detail: '邀请可信任的同事、经理或教练对关键判断提供反馈。' },
          ],
          processTitle: '如何完成领导力自评',
          process: [
            { title: '明确角色要求', detail: '先理解当前和下一阶段角色真正要求哪些领导行为。' },
            { title: '独立评分并举证', detail: '在查看他人意见前先记录自己的判断和事实依据。' },
            { title: '寻找矛盾证据', detail: '主动回顾与自我判断不一致的结果和反馈，避免确认偏差。' },
            { title: '请求外部反馈', detail: '比较自我认知与他人体验，识别最值得探索的差异。' },
            { title: '形成发展重点', detail: '选择少量高影响行为，安排实践机会和复盘。' },
          ],
          useCases: ['发展计划启动', '晋升或角色转换准备', '教练会谈前准备', '阶段性领导行为复盘'],
          pitfalls: [
            { title: '只有分数没有案例', detail: '缺少事实证据的评分无法支持有效发展决策。' },
            { title: '把自评当客观事实', detail: '自评必须与外部反馈和实际结果交叉验证。' },
            { title: '发展重点过多', detail: '同时改变大量能力会稀释注意力，应优先选择高影响行为。' },
          ],
          managerApplication: [
            '先请员工解释自评分数背后的证据，再分享经理观察，避免一开始就宣布正确答案。',
            '重点讨论双方判断差异最大的项目，因为差异通常比平均分更能产生发展洞察。',
          ],
          keyTakeaway: '自我评估是建立觉察的起点，必须通过证据、外部反馈和行动进一步验证。',
        },
      },
      {
        id: 'belbin-team-roles',
        path: toolPath('feedback-assessment', 'belbin-team-roles'),
        title: 'Belbin Team Roles',
        summary: '理解成员在推动、协同、创新、执行和质量保障中的自然贡献。',
        summaryEnglish: 'Understand each member\'s natural contribution to momentum, collaboration, innovation, execution, and quality.',
        application: '适合组建团队、角色分工和协作冲突讨论。',
        icon: 'team',
        detail: {
          subtitle: '从互补贡献而不是“谁最好”出发，理解团队为何平衡或失衡。',
          overviewTitle: '什么是 Belbin 团队角色？',
          overview: [
            'Belbin 团队角色模型描述人们在团队协作中倾向展现的九类贡献。成功团队通常不是每个人都相似，而是能够覆盖思考、关系和行动三类互补角色。',
            '每种角色既有优势，也有与优势伴生的可接受短板。模型用于理解协作和设计团队，不应用来给人贴固定标签或替代岗位能力判断。',
          ],
          frameworkTitle: '三类团队贡献',
          framework: [
            { title: 'Action-oriented · 行动导向', detail: '推动者、执行者和完成者分别提供节奏、落地和质量保障。' },
            { title: 'People-oriented · 人际导向', detail: '协调者、团队工作者和资源调查者连接目标、关系与外部机会。' },
            { title: 'Thought-oriented · 思考导向', detail: '智多星、监督评估者和专家提供创意、判断和深度知识。' },
          ],
          processTitle: '如何在团队中使用角色模型',
          process: [
            { title: '识别自然贡献', detail: '结合自评、同事观察和真实项目行为判断常见角色。' },
            { title: '绘制团队分布', detail: '查看哪些贡献重复过多，哪些关键贡献长期缺失。' },
            { title: '连接当前任务', detail: '不同项目阶段需要不同角色组合，应围绕任务而不是标签分工。' },
            { title: '调整协作方式', detail: '明确互补关系、潜在摩擦和需要由成员主动补位的部分。' },
          ],
          useCases: ['项目团队组建', '团队协作复盘', '冲突理解', '角色和任务重新分配'],
          pitfalls: [
            { title: '把偏好当能力', detail: '自然偏好不等于熟练表现，仍需观察实际贡献质量。' },
            { title: '给成员永久贴标签', detail: '角色会随情境和任务变化，不能限制人员发展。' },
            { title: '追求九类角色各一人', detail: '一人可能承担多种贡献，关键是任务需要被覆盖。' },
          ],
          managerApplication: [
            '经理可以用团队角色语言讨论贡献差异，将“他总是反对”转化为“他在提供风险判断，但表达方式需要调整”。',
            '分配任务时同时考虑岗位职责、实际能力和团队角色，不应只依据测评偏好。',
          ],
          keyTakeaway: '团队平衡来自关键贡献被覆盖并有效协同，而不是成员具有同一种优秀特质。',
        },
      },
      {
        id: 'soft-skills-assessment',
        path: toolPath('feedback-assessment', 'soft-skills-assessment'),
        title: 'Soft Skills Assessment',
        summary: '评估沟通、影响、协作、适应和问题解决等跨岗位能力。',
        summaryEnglish: 'Assess transferable capabilities such as communication, influence, collaboration, adaptability, and problem solving.',
        application: '适合管理者发展和跨团队工作能力诊断。',
        icon: 'assessment',
        detail: {
          subtitle: '用行为证据评估影响工作结果的人际能力，而不是依赖模糊印象。',
          overviewTitle: '什么是软技能评估？',
          overview: [
            '软技能包括沟通、情绪理解、协作、适应、冲突处理、影响和复杂问题解决等跨岗位行为能力。它们高度依赖情境，因此比技术知识更难评估。',
            '可靠评估需要明确行为标准，并结合自评、经理观察、同事反馈和真实工作案例。抽象的“沟通好不好”很难行动，具体行为和影响才有发展价值。',
          ],
          frameworkTitle: '常见软技能维度',
          framework: [
            { title: '沟通', detail: '清楚表达、主动倾听，并根据对象和场景调整信息。' },
            { title: '情绪智能', detail: '识别和管理自身情绪，理解他人感受与关系影响。' },
            { title: '协作', detail: '分享信息、建立共识，并为共同结果承担责任。' },
            { title: '适应', detail: '在变化、模糊和优先级调整中保持有效行动。' },
            { title: '问题解决', detail: '在信息不完整时结构化分析、提出选择并验证结果。' },
            { title: '影响', detail: '在不依赖职位权力的情况下建立认同和推动行动。' },
          ],
          processTitle: '如何评估软技能',
          process: [
            { title: '选择关键情境', detail: '围绕岗位最重要的真实场景选择需要评估的软技能。' },
            { title: '定义行为指标', detail: '描述不同水平下能够观察到的具体行为与结果。' },
            { title: '收集多源证据', detail: '综合案例、结果、自评和不同合作方的观察。' },
            { title: '设计行为实践', detail: '选择一个高频场景反复实践，并及时获取反馈。' },
          ],
          useCases: ['管理者发展', '跨团队协作改善', '高潜人才评估', '沟通与影响能力建设'],
          pitfalls: [
            { title: '使用人格标签', detail: '“不够外向”不是行为标准，应描述实际场景中的影响。' },
            { title: '只靠一次观察', detail: '软技能受情境影响，需要跨场景和多来源证据。' },
            { title: '脱离工作练习', detail: '软技能应在真实任务和关系中发展，而不是只完成课程。' },
          ],
          managerApplication: [
            '反馈软技能时采用“情境、行为、影响”的表达，并邀请员工补充自己的视角。',
            '把发展要求连接到一个真实、重复发生的工作场景，便于观察变化和及时反馈。',
          ],
          keyTakeaway: '软技能评估的可信度来自具体行为、多源证据和真实场景。',
        },
      },
      {
        id: 'workplace-skills-assessment',
        path: toolPath('feedback-assessment', 'workplace-skills-assessment'),
        title: 'Workplace Skills Assessment',
        summary: '围绕具体岗位与场景评估完成工作所需的实践能力。',
        summaryEnglish: 'Assess the practical capabilities required to perform effectively in a specific role and work context.',
        application: '适合岗位胜任、入岗准备和发展资源配置。',
        icon: 'skills',
        detail: {
          subtitle: '从岗位真实任务出发，判断人员能够做什么，以及下一步需要发展什么。',
          overviewTitle: '什么是岗位技能评估？',
          overview: [
            '岗位技能评估关注个人和团队在具体工作环境中完成任务所需的实践能力，包括技术技能、流程知识、数字能力、分析判断和岗位专业知识。',
            '高质量评估必须与岗位情境相匹配、以证据为基础，并能够直接连接发展行动。通用问卷很难反映不同角色真正需要的能力。',
          ],
          frameworkTitle: '评估设计的五个组成',
          framework: [
            { title: '岗位能力', detail: '从关键任务中提炼技术、流程、判断和协作能力。' },
            { title: '熟练度标准', detail: '定义入门、发展中、胜任、熟练和专家等不同水平的行为。' },
            { title: '匹配的方法', detail: '根据技能性质选择实操、案例、测试、作品或观察。' },
            { title: '多源数据', detail: '结合自评、经理判断、同行意见和客观绩效证据。' },
            { title: '发展连接', detail: '将差距转化为任务实践、教练、轮岗、课程或资源支持。' },
          ],
          processTitle: '如何设计岗位技能评估',
          process: [
            { title: '拆解关键任务', detail: '与业务负责人和专家识别真正影响结果的工作任务。' },
            { title: '定义熟练度', detail: '为每项能力写出不同水平下可观察的行为和质量标准。' },
            { title: '选择评估方式', detail: '尽可能使用接近真实工作的任务或情境。' },
            { title: '交叉验证', detail: '比较多种数据来源，识别一致结论和需要继续核实的差异。' },
            { title: '形成发展计划', detail: '按业务影响排序差距，并配置最合适的发展方式。' },
          ],
          useCases: ['岗位胜任评估', '入岗和转岗准备', '继任规划', '培训需求分析'],
          pitfalls: [
            { title: '能力定义过于通用', detail: '标准必须反映具体岗位任务和组织环境。' },
            { title: '评估方式脱离实践', detail: '书面知识测试无法充分证明复杂工作的实际能力。' },
            { title: '评估后没有发展动作', detail: '数据只有连接资源、任务和跟进机制时才产生价值。' },
          ],
          managerApplication: [
            '在反馈技能差距前先说明岗位任务和熟练度标准，再讨论员工当前证据。',
            '区分“尚未有机会展示”和“已有机会但表现不足”，两种情况需要不同管理行动。',
          ],
          keyTakeaway: '岗位技能评估必须贴近真实任务，并将结果直接连接到发展或人员决策。',
        },
      },
    ],
    workflow: [
      { title: '多源观察', detail: '避免用单一评价者代表完整事实。' },
      { title: '聚焦行为', detail: '描述可观察行为及其影响，而非人格标签。' },
      { title: '形成重点', detail: '从大量反馈中选择最有业务价值的改变。' },
    ],
    ctaTitle: '把评估结果转化为高质量反馈',
    ctaDescription: '在工作台中练习如何陈述事实、承接情绪并与员工共同形成改进计划。',
    accent: '#005691',
    accentSoft: '#eaf3f8',
  },
  'development-frameworks': {
    slug: 'development-frameworks',
    path: '/solutions/development-frameworks',
    navLabel: '发展框架',
    navLabelEnglish: 'Development Framework',
    title: '发展框架：理解专业人士与组织如何成长',
    titleEnglish: 'Development frameworks: understand how professionals and organizations grow',
    summary: '理解职业与管理能力如何成长，识别角色转换中的断点，并设计更匹配的发展路径。',
    summaryEnglish: 'Understand how careers and leadership capabilities grow, then design development paths that fit the transition.',
    heroImage: `${LANDING_ASSET}/solution-category-development-premium-202609-v2.webp`,
    heroImageAlt: '一组专业人士在发展研讨现场专注聆听',
    introTitle: '理解成长规律，才能选择正确的发展方式',
    introParagraphs: [
      '实践工具告诉我们如何解决问题，发展框架则解释能力如何形成、为何停滞，以及角色变化会带来哪些新的要求。理解这些规律能够减少“一次培训解决所有问题”的误区。',
      '彼得原理提醒组织关注晋升后的能力变化，管理模型帮助理解不同领导方式，职业发展模型则把经历、反馈、学习和机会连接为长期成长路径。',
    ],
    toolsHeading: '发展框架与模型',
    toolsHeadingEnglish: 'Development frameworks and models',
    tools: [
      {
        id: 'peter-principle',
        path: toolPath('development-frameworks', 'peter-principle'),
        title: 'Peter Principle',
        summary: '理解为什么在原岗位表现优秀的人，晋升后可能面对全新的能力缺口。',
        summaryEnglish: 'Understand why strong performance in one role can still lead to new capability gaps after promotion.',
        application: '适合晋升决策、入岗支持和管理者转型设计。',
        icon: 'leadership',
        detail: {
          subtitle: '当前岗位的优秀表现并不自动证明下一岗位的胜任能力，晋升需要评估未来要求。',
          overviewTitle: '什么是彼得原理？',
          overview: [
            '彼得原理指出，组织常根据员工在当前岗位的表现进行晋升，直到员工进入一个自己尚不能胜任的岗位。问题不在于员工突然失去能力，而是新岗位要求发生了变化。',
            '专业贡献者依赖技术深度和个人产出，管理者则需要授权、反馈、教练、判断和团队发展。若组织只奖励当前绩效，却不评估未来能力和提供过渡支持，就会放大晋升风险。',
          ],
          frameworkTitle: '晋升风险的三个来源',
          framework: [
            { title: '用当前表现预测未来', detail: '过去成功的能力可能不是新角色最重要的能力。' },
            { title: '只有一条发展通道', detail: '把管理岗位作为唯一奖励，会迫使优秀专业人才离开最擅长的工作。' },
            { title: '缺少角色过渡支持', detail: '新经理没有获得明确要求、实践、反馈和教练，只能独自摸索。' },
          ],
          processTitle: '如何降低彼得原理风险',
          process: [
            { title: '定义下一角色能力', detail: '明确新岗位与当前岗位在任务、关系和决策上的关键变化。' },
            { title: '评估未来潜力', detail: '通过真实情境、行为证据和发展速度判断准备度。' },
            { title: '先提供试验机会', detail: '用代理任务、项目领导或短期轮岗验证能力和意愿。' },
            { title: '配置转型支持', detail: '在晋升后提供结构化入岗、教练、同行支持和及时反馈。' },
            { title: '建立双通道', detail: '让专业深化与管理发展都能获得认可、影响力和回报。' },
          ],
          useCases: ['晋升和继任决策', '新经理入岗', '专业与管理双通道设计', '晋升后表现问题诊断'],
          pitfalls: [
            { title: '把转型困难归因于态度', detail: '先检查新角色要求是否清晰，以及员工是否获得必要支持。' },
            { title: '只评估意愿', detail: '想做经理不代表已准备好，需要观察未来岗位行为。' },
            { title: '晋升后立即放手', detail: '角色转换期需要更密集的反馈和明确边界。' },
          ],
          managerApplication: [
            '与晋升候选人讨论新岗位实际工作，而不是只谈头衔、待遇和认可。',
            '对于晋升后遇到困难的员工，区分能力差距、角色不匹配和支持不足，再决定发展或岗位调整。',
          ],
          keyTakeaway: '好的晋升决策评估的是下一岗位的能力与意愿，并为角色转换提供持续支持。',
        },
      },
      {
        id: 'management-models',
        path: toolPath('development-frameworks', 'management-models'),
        title: 'Management Models',
        summary: '用不同管理模型理解领导行为、团队需求和组织情境之间的关系。',
        summaryEnglish: 'Use different management models to understand the relationship among leadership behavior, team needs, and organizational context.',
        application: '适合管理者培养、领导方式复盘和团队情境分析。',
        icon: 'strategy',
        detail: {
          subtitle: '建立一组互补的管理视角，根据人员、任务和环境有意识地调整领导行为。',
          overviewTitle: '为什么需要管理模型？',
          overview: [
            '管理不是单一技能。不同模型分别解释领导者如何适应员工成熟度、平衡结果与关系、激发改变，以及通过服务团队建立信任。',
            '理解模型不是为了寻找唯一正确答案，而是为了增加诊断语言和行为选择。优秀经理会根据情境组合使用不同视角。',
          ],
          frameworkTitle: '四种有代表性的管理视角',
          framework: [
            { title: '情境领导', detail: '根据员工在具体任务上的能力和投入程度，在指导、教练、支持和授权之间调整。' },
            { title: '交易与变革型领导', detail: '同时运用清晰要求和责任机制，以及愿景、意义和高标准激发额外投入。' },
            { title: '服务型领导', detail: '把团队成长、支持和障碍清除放在管理角色的中心。' },
            { title: '管理方格', detail: '从关注结果和关注人员两个维度审视管理风格及其平衡。' },
          ],
          processTitle: '如何应用管理模型',
          process: [
            { title: '诊断具体情境', detail: '明确任务复杂度、风险、员工能力、投入和时间压力。' },
            { title: '识别默认风格', detail: '观察自己在压力下最常使用的管理方式及其副作用。' },
            { title: '选择互补行为', detail: '依据情境有意识地增加指导、支持、授权或挑战。' },
            { title: '观察并调整', detail: '根据员工反应和结果检验方式是否有效，持续校准。' },
          ],
          useCases: ['新经理培养', '团队成员差异化管理', '变革领导', '管理风格反馈'],
          pitfalls: [
            { title: '追求唯一最佳风格', detail: '相同方式不会适合所有人员、任务和发展阶段。' },
            { title: '用模型给人贴标签', detail: '模型用于选择行为，不用于固定定义经理或员工。' },
            { title: '只理解概念不练行为', detail: '管理能力需要在真实沟通、反馈和授权中反复实践。' },
          ],
          managerApplication: [
            '在一次困难沟通前，明确当前最需要的是设定边界、提供支持、激发思考还是授权行动。',
            '复盘时比较自己的意图与员工的实际体验，识别管理方式需要如何调整。',
          ],
          keyTakeaway: '管理模型提供的是可选择的行为视角，真正的能力在于准确诊断并灵活使用。',
        },
      },
      {
        id: 'professional-development',
        path: toolPath('development-frameworks', 'professional-development'),
        title: 'Professional-Development Models',
        summary: '连接职业阶段、关键经历、能力积累和发展机会，形成长期成长地图。',
        summaryEnglish: 'Connect career stages, pivotal experiences, capability building, and development opportunities in a long-term growth map.',
        application: '适合职业规划、人才梯队和个性化发展路径设计。',
        icon: 'development',
        detail: {
          subtitle: '理解能力如何随经验、反思和职业阶段演进，为不同发展阶段选择不同支持。',
          overviewTitle: '为什么专业发展模型重要？',
          overview: [
            '职业和能力发展并非直线过程，而是包含学习、平台期、角色转换和重新定位。发展模型帮助个人和组织理解这些规律。',
            '这些模型不能直接替代发展计划，但能解释不同阶段需要什么：初学者需要结构，熟练者需要复杂挑战，经验需要反思才能沉淀，而职业诉求也会随阶段变化。',
          ],
          frameworkTitle: '四种重要的发展模型',
          framework: [
            { title: 'Dreyfus 技能发展', detail: '能力从新手、进阶新手、胜任、熟练到专家，判断方式从遵循规则逐渐转向整体理解。' },
            { title: 'Kolb 经验学习循环', detail: '有效学习需要经历具体体验、反思观察、形成概念并再次主动试验。' },
            { title: '70:20:10', detail: '强调发展主要来自真实工作经历，其次来自他人互动，并由正式学习提供必要知识。' },
            { title: '职业阶段模型', detail: '探索、建立、保持与转型等阶段具有不同的发展诉求、机会和风险。' },
          ],
          processTitle: '如何把发展模型用于实践',
          process: [
            { title: '判断当前阶段', detail: '识别员工在目标能力和职业旅程中的真实位置。' },
            { title: '选择匹配挑战', detail: '让任务难度略高于当前稳定水平，同时提供必要结构。' },
            { title: '组合发展方式', detail: '围绕真实任务配置反馈、教练、同行学习和正式资源。' },
            { title: '完成学习循环', detail: '安排反思和下一次试验，避免经验只被重复而没有沉淀。' },
            { title: '定期重新判断', detail: '根据新证据和诉求变化调整发展方向与支持方式。' },
          ],
          useCases: ['个性化发展计划', '学习项目设计', '职业阶段对话', '人才梯队和继任发展'],
          pitfalls: [
            { title: '所有人使用同一方案', detail: '不同能力水平和职业阶段需要不同挑战与支持。' },
            { title: '把上课等同发展', detail: '知识输入需要真实应用、反馈和反思才能形成能力。' },
            { title: '经验没有复盘', detail: '重复工作不一定带来成长，必须提炼规律并尝试新行为。' },
          ],
          managerApplication: [
            '与员工设计发展计划时，先判断其当前阶段和下一步关键经历，再选择课程或资源。',
            '为重要任务安排开始前准备、过程反馈和完成后复盘，让工作本身成为发展载体。',
          ],
          keyTakeaway: '发展不是活动数量，而是匹配阶段的挑战、反馈、反思和再次实践。',
        },
      },
    ],
    workflow: [
      { title: '先看阶段', detail: '同一种发展方式不适合所有角色与时期。' },
      { title: '用经历发展', detail: '把真实任务作为能力形成的核心载体。' },
      { title: '提供支撑', detail: '用反馈、教练和资源降低角色转换风险。' },
    ],
    ctaTitle: '为下一次发展对话做好准备',
    ctaDescription: '把角色要求、员工诉求和当前表现放进同一场沟通，并通过预演检验表达效果。',
    accent: '#8a2d62',
    accentSoft: '#f7eaf1',
  },
} satisfies Record<ToolkitPageSlug, ToolkitPageConfig>;

export interface ToolkitToolVisual {
  image: string;
  imageAlt: string;
  mobileObjectPosition: string;
}

export const toolkitToolVisuals: Readonly<Record<string, ToolkitToolVisual>> = {
  'swot-analysis': {
    image: `${LANDING_ASSET}/method-swot-analysis-premium-202609.webp`,
    imageAlt: '四名设计人员围绕建筑蓝图讨论方案',
    mobileObjectPosition: '58% center',
  },
  'vrio-analysis': {
    image: `${LANDING_ASSET}/method-vrio-analysis-premium-202609.webp`,
    imageAlt: '工程师在实验室调试精密电子设备',
    mobileObjectPosition: '66% center',
  },
  'mckinsey-7s': {
    image: `${LANDING_ASSET}/method-mckinsey-7s-premium-202609.webp`,
    imageAlt: '三名工程师在汽车测试设备旁协同讨论',
    mobileObjectPosition: '72% center',
  },
  'skills-gap-analysis': {
    image: `${LANDING_ASSET}/method-skills-gap-analysis-premium-202609.webp`,
    imageAlt: '两名工程师使用仪表检测电子电路板',
    mobileObjectPosition: '60% center',
  },
  'grow-model': {
    image: `${LANDING_ASSET}/method-grow-model-premium-202609.webp`,
    imageAlt: '一名员工在一对一交流中专注聆听对方',
    mobileObjectPosition: '62% center',
  },
  'smart-goals': {
    image: `${LANDING_ASSET}/method-smart-goals-premium-202609.webp`,
    imageAlt: '工程人员在工作台上精确测量金属构件',
    mobileObjectPosition: '64% center',
  },
  'career-coaching-tools': {
    image: `${LANDING_ASSET}/method-career-coaching-tools-premium-202609.webp`,
    imageAlt: '多名行人沿现代建筑廊道向前行走',
    mobileObjectPosition: '31% center',
  },
  '360-degree-feedback': {
    image: `${LANDING_ASSET}/method-360-degree-feedback-premium-202609.webp`,
    imageAlt: '多位同事围绕同一议题交换不同视角',
    mobileObjectPosition: '40% center',
  },
  'leadership-self-assessment': {
    image: `${LANDING_ASSET}/method-leadership-self-assessment-premium-202609.webp`,
    imageAlt: '管理者独自在落地窗前沉思',
    mobileObjectPosition: '52% center',
  },
  'belbin-team-roles': {
    image: `${LANDING_ASSET}/method-belbin-team-roles-premium-202609.webp`,
    imageAlt: '三名工程师在汽车原型设备旁协作分析',
    mobileObjectPosition: '73% center',
  },
  'soft-skills-assessment': {
    image: `${LANDING_ASSET}/method-soft-skills-assessment-premium-202609.webp`,
    imageAlt: '一名员工专注倾听同事表达观点',
    mobileObjectPosition: '52% center',
  },
  'workplace-skills-assessment': {
    image: `${LANDING_ASSET}/method-workplace-skills-assessment-premium-202609.webp`,
    imageAlt: '两名工程师近距离检查并调整电路板',
    mobileObjectPosition: '85% center',
  },
  'peter-principle': {
    image: `${LANDING_ASSET}/method-peter-principle-premium-202609.webp`,
    imageAlt: '一位专业人员沿现代建筑楼梯向上行走',
    mobileObjectPosition: '62% center',
  },
  'management-models': {
    image: `${LANDING_ASSET}/method-management-models-premium-202609.webp`,
    imageAlt: '两位专业人员共同审阅大型工程规划图',
    mobileObjectPosition: '62% center',
  },
  'professional-development': {
    image: `${LANDING_ASSET}/method-professional-development-premium-202609.webp`,
    imageAlt: '两位工程师在实验室共同操作医疗机器人',
    mobileObjectPosition: '62% center',
  },
};

function validateToolkitToolVisuals() {
  const toolIds = new Set<string>();
  for (const page of Object.values(toolkitPages)) {
    for (const tool of page.tools) {
      if (toolIds.has(tool.id)) {
        throw new Error(`Duplicate toolkit tool id: ${tool.id}`);
      }
      toolIds.add(tool.id);
      const visual = toolkitToolVisuals[tool.id];
      if (!visual?.image.startsWith('/assets/') || !visual.imageAlt.trim()) {
        throw new Error(`Missing local visual for toolkit tool: ${tool.id}`);
      }
    }
  }
}

validateToolkitToolVisuals();

export const toolkitPageList = Object.values(toolkitPages);

export function isToolkitPageSlug(value: string | undefined): value is ToolkitPageSlug {
  return Boolean(value && Object.prototype.hasOwnProperty.call(toolkitPages, value));
}

export function findToolkitTool(page: ToolkitPageConfig, toolId: string | undefined) {
  return toolId ? page.tools.find((tool) => tool.id === toolId) : undefined;
}

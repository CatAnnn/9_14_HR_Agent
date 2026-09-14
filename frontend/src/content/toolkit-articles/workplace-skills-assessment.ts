export type WsaSource = {
  title: string;
  url: string;
  accessed: string;
};

export type WsaContentLink = {
  href: string;
  label: string;
};

export type WsaProcessStep = {
  id: string;
  title: string;
  premise: string;
  output: string;
  checks: string[];
};

export type WsaRubricLevel = {
  level: string;
  name: string;
  signal: string;
  evidence: string;
};

export type WsaMatrixRow = {
  method: string;
  bestFor: string;
  evidence: string;
  controls: string;
};

const accessed = "2026-08-11";

export const workplaceSkillsAssessmentArticle = {
  title: "工作场景技能评估工具包",
  kicker: "Workplace Skills Assessment",
  deck:
    "把岗位、任务、技能、证据、差距与发展路线放在同一张经营地图上，让技能评估不再是问卷填报，而是可验证、可讨论、可投资的组织能力系统。",
  sourceNote:
    "综合参考 SkillsFuture Skills Framework、OPM 岗位分析与测评资料、O*NET 内容模型、SFIA 9 责任等级、CCL 360 反馈公开说明；访问日期：2026-08-11。",
  contents: [
    { href: "#wsa-operating-model", label: "六段式主线" },
    { href: "#wsa-role-canvas", label: "岗位画布" },
    { href: "#wsa-architecture", label: "技术/核心架构" },
    { href: "#wsa-rubrics", label: "等级标尺" },
    { href: "#wsa-methods", label: "方法矩阵" },
    { href: "#wsa-work-sample", label: "真实工作样本" },
    { href: "#wsa-portfolio", label: "证据组合" },
    { href: "#wsa-gap", label: "差距优先级" },
    { href: "#wsa-development", label: "发展与治理" },
  ] satisfies WsaContentLink[],
  principles: [
    "先定义工作，再评估人。每一项技能必须回到关键任务、绩效后果和工作情境。",
    "以证据替代印象。自评可以启动讨论，但不能单独决定能力等级。",
    "区分当前胜任、未来准备与组织稀缺度。三者都重要，但管理动作不同。",
    "用共同语言连接招聘、绩效、学习与调配，同时允许业务单元保留情境化描述。",
  ],
  processSteps: [
    {
      id: "job",
      title: "Job：界定岗位边界",
      premise:
        "从业务目标、服务对象、决策权限、交付物和工作环境开始，而不是从现成职位名称开始。",
      output:
        "形成一页岗位定义：使命、关键成果、工作条件、协作界面、不可妥协的合规要求。",
      checks: ["岗位是否仍然存在于未来 12-24 个月的业务模型中", "是否区分岗位族、岗位、具体职位", "是否写清成功失败的业务后果"],
    },
    {
      id: "tasks",
      title: "Tasks：拆解关键任务",
      premise:
        "借鉴 OPM 岗位分析逻辑，把工作拆成可观察任务，并标注频率、重要性、难度和错误成本。",
      output:
        "任务清单和任务权重：日常任务、关键事件任务、低频高风险任务分别进入不同评估场景。",
      checks: ["任务是否可被观察或验证", "是否覆盖异常处理和跨团队交付", "是否避免把态度词误写成任务"],
    },
    {
      id: "skills",
      title: "Skills：映射技能语言",
      premise:
        "参照 SkillsFuture 的技术技能与通用/核心技能分层，以及 O*NET 对工作者要求、知识、能力和任务的区分。",
      output:
        "技能库条目：技能名称、定义、适用任务、熟练表现、反例、可迁移范围和更新周期。",
      checks: ["技能是否连接至少一项关键任务", "是否区分知识、操作、判断与协作", "是否能被非 HR 的业务专家理解"],
    },
    {
      id: "evidence",
      title: "Evidence：采集多源证据",
      premise:
        "把工作样本、模拟、成果档案、结构化访谈、主管观察、同伴反馈和客户反馈组合使用。",
      output:
        "证据组合包：每项技能至少有一种直接证据；高风险技能需要两种以上互补证据。",
      checks: ["证据是否来自真实或高度相似的工作", "评分者是否接受校准", "是否保留评分依据而不是只保留分数"],
    },
    {
      id: "gap",
      title: "Gap：判断差距优先级",
      premise:
        "差距不是分数低就优先。优先级取决于业务风险、未来需求、可发展性和供给稀缺度。",
      output:
        "差距热力图：立即补位、重点培养、观察储备、低优先级四类动作。",
      checks: ["是否区分个人差距与岗位设计问题", "是否标注差距证据强度", "是否避免把一次失误固化为能力标签"],
    },
    {
      id: "development",
      title: "Development：连接发展路线",
      premise:
        "评估的终点不是报告，而是任务机会、导师反馈、课程、认证、轮岗和绩效目标的组合。",
      output:
        "90 天行动计划和 6 个月复盘节奏：每条发展动作都绑定目标技能、练习场景、支持者和验证点。",
      checks: ["是否把学习项目连接到真实任务", "是否规定复评窗口", "是否让员工看到横向与纵向路径"],
    },
  ] satisfies WsaProcessStep[],
  roleCanvas: [
    {
      label: "岗位使命",
      prompt: "这个岗位为哪类客户或内部流程创造什么结果？",
      example: "把“负责数据分析”改写为“把分散经营数据转化为可执行的补货与定价建议”。",
    },
    {
      label: "关键任务",
      prompt: "哪些任务决定 70% 的绩效差异、风险暴露或客户体验？",
      example: "列出 8-12 项任务，标注频率、重要性、难度和失败成本。",
    },
    {
      label: "技能单元",
      prompt: "完成任务需要哪些技术技能、核心技能、领域知识和判断模式？",
      example: "同一任务可同时需要数据建模、商业解释、冲突沟通和质量控制。",
    },
    {
      label: "证据方式",
      prompt: "用什么证据证明一个人能在工作情境中稳定表现？",
      example: "优先使用工作样本、成果档案和校准观察；问卷只作为低成本补充。",
    },
    {
      label: "发展路径",
      prompt: "如果未达标，最短的有效练习路径是什么？",
      example: "课程学习解决知识缺口；跟岗和任务委派解决情境判断；教练反馈解决行为盲区。",
    },
    {
      label: "治理边界",
      prompt: "哪些评估结论可用于发展，哪些可用于选拔、晋升或合规记录？",
      example: "高影响人事决策必须有岗位相关性、评分校准和申诉机制。",
    },
  ],
  architecture: {
    technical: [
      "岗位专属工具、系统、方法、标准和流程能力",
      "领域知识：产品、工艺、法规、客户、数据口径",
      "工作产出能力：分析、设计、操作、审查、交付和改进",
      "风险控制能力：安全、质量、合规、信息保护和异常升级",
    ],
    core: [
      "认知能力：问题定义、证据推理、优先级和学习迁移",
      "协作能力：沟通、影响、冲突处理、跨职能推进",
      "自我管理：可靠交付、反馈吸收、压力下判断",
      "领导潜力：发展他人、塑造环境、承担更大范围责任",
    ],
    note:
      "技术技能回答“会不会做这项工作”，核心技能回答“能否在复杂组织中持续做成”。二者必须在同一任务场景中观察，不能拆成互不相干的测评项目。",
  },
  internalRubric: [
    {
      level: "L1",
      name: "了解",
      signal: "能复述概念和步骤，在明确指令下完成局部动作。",
      evidence: "知识测验、跟岗记录、低风险练习任务。",
    },
    {
      level: "L2",
      name: "执行",
      signal: "能按标准流程独立完成常规任务，知道何时升级。",
      evidence: "标准作业样本、错误率、主管抽查记录。",
    },
    {
      level: "L3",
      name: "诊断",
      signal: "能处理非例行情况，解释取舍，并把问题拆成可行动方案。",
      evidence: "情境模拟、案例复盘、跨团队问题单。",
    },
    {
      level: "L4",
      name: "优化",
      signal: "能改进流程或方法，指导他人，并稳定提升团队产出。",
      evidence: "改进项目、同伴反馈、前后指标对比。",
    },
    {
      level: "L5",
      name: "塑造",
      signal: "能定义标准、影响资源配置，在不确定条件下建立新的能力系统。",
      evidence: "能力路线图、治理机制、业务影响评估。",
    },
  ] satisfies WsaRubricLevel[],
  sfiaReference: {
    note:
      "以下是单独的 SFIA 9 七级责任参考，用于校准数字与知识密集型岗位的责任范围。它不是本工具包五级内部标尺的换算表，也不代表两套等级可以直接等同。",
    levels: [
      "1 Follow：在密切指导下完成例行任务。",
      "2 Assist：在常规监督下协助他人，处理常见问题。",
      "3 Apply：在一般方向下完成多样任务，管理自己的交付。",
      "4 Enable：自主完成复杂活动，并支持或指导他人。",
      "5 Ensure / advise：提供权威建议，对重要工作结果负责。",
      "6 Initiate / influence：影响组织层面决策、政策和协作。",
      "7 Set strategy / inspire / mobilise：设定愿景和战略，对整体成功承担责任。",
    ],
  },
  methodMatrix: [
    {
      method: "工作样本",
      bestFor: "入职即需具备、错误成本较高、可被真实复现的任务。",
      evidence: "完成品质量、过程观察、错误类型、时间与资源使用。",
      controls: "任务来自岗位分析；评分表先校准；材料定期更新。",
    },
    {
      method: "情境模拟",
      bestFor: "客户沟通、异常处置、跨部门推进等复杂互动。",
      evidence: "行为选择、追问质量、利益相关者处理、升级判断。",
      controls: "使用统一脚本；训练观察员；避免表演能力替代岗位能力。",
    },
    {
      method: "成果档案",
      bestFor: "项目型、创意型、专家型岗位的长期能力判断。",
      evidence: "交付物、业务结果、复盘材料、角色贡献说明。",
      controls: "要求说明上下文和个人贡献；抽样核验；避免只看包装质量。",
    },
    {
      method: "结构化访谈",
      bestFor: "验证经验深度、判断逻辑和价值冲突处理。",
      evidence: "具体情境、行动、依据、结果和反思。",
      controls: "问题固定；评分锚点明确；追问围绕证据而非好感。",
    },
    {
      method: "360 反馈",
      bestFor: "领导、协作、影响力和自我认知类能力发展。",
      evidence: "多角色评分、重要性权重、书面评论、个人行动计划。",
      controls: "用于发展优先；保护反馈心理安全；结合实际成果校验。",
    },
  ] satisfies WsaMatrixRow[],
  workSample: {
    title: "真实工作样本：客户升级问题的 45 分钟处理",
    scenario:
      "适用于客户成功、运营经理、项目经理或一线主管。候选人收到一组真实化材料：客户投诉摘要、合同条款、服务数据、内部工单、两封语气紧张的邮件和资源限制说明。",
    task:
      "45 分钟内提交一页处置方案，并进行 8 分钟口头说明。方案必须说明事实判断、风险排序、客户沟通、内部协同、下一步验证和升级条件。",
    materials: ["客户背景与商业价值", "服务水平记录", "内部责任边界", "可调用资源", "历史沟通片段"],
    scoring: [
      "问题定义：是否区分事实、假设和情绪信号。",
      "任务技能：是否能使用数据、流程和合同信息形成可执行方案。",
      "核心技能：是否兼顾客户信任、内部可行性和清晰沟通。",
      "风险判断：是否识别合规、收入、声誉和交付风险。",
      "复盘意识：是否提出可验证的后续指标，而不是一次性安抚。",
    ],
  },
  portfolio: [
    {
      layer: "直接表现",
      examples: "工作样本、现场观察、模拟任务、真实交付物。",
      value: "最能证明能否做成任务。",
    },
    {
      layer: "结果证据",
      examples: "质量、周期、客户、收入、成本、风险指标。",
      value: "说明能力是否转化为业务结果。",
    },
    {
      layer: "过程证据",
      examples: "决策记录、复盘、变更说明、同伴协作记录。",
      value: "解释结果背后的判断质量。",
    },
    {
      layer: "反馈证据",
      examples: "主管、同伴、客户、下属或项目干系人反馈。",
      value: "补足协作和领导行为的可见性。",
    },
  ],
  gapPriority: [
    {
      name: "立即补位",
      rule: "高业务风险、高未来需求、当前证据不足。",
      action: "短周期训练、任务陪跑、外部招聘或临时专家支持同步启动。",
    },
    {
      name: "重点培养",
      rule: "高未来需求、可发展性强、当前缺口中等。",
      action: "项目委派、导师、课程和 90 天复评组合。",
    },
    {
      name: "观察储备",
      rule: "需求可能上升但证据尚不充分。",
      action: "增加暴露机会，积累成果档案，避免过早贴标签。",
    },
    {
      name: "低优先级",
      rule: "对岗位结果影响小，或可通过流程/工具替代。",
      action: "不进入高成本发展项目，改用工作辅助或团队互补。",
    },
  ],
  developmentRoutes: [
    {
      route: "任务内发展",
      use: "缺口来自情境经验不足。",
      pattern: "影子学习 -> 共同交付 -> 独立交付 -> 复盘指导。",
    },
    {
      route: "课程与认证",
      use: "缺口来自知识、标准或工具体系。",
      pattern: "短课输入 -> 练习任务 -> 工作样本验证 -> 证据入档。",
    },
    {
      route: "导师与教练",
      use: "缺口来自判断、影响力或领导行为。",
      pattern: "反馈解读 -> 行为目标 -> 关键场景练习 -> 多源复评。",
    },
    {
      route: "岗位轮换",
      use: "缺口来自系统视角和跨职能理解。",
      pattern: "明确学习目标 -> 限定任务包 -> 双主管反馈 -> 回岗应用。",
    },
  ],
  governance: [
    {
      title: "标准治理",
      body:
        "建立技能命名、定义、任务映射、证据类型和等级锚点的版本管理。业务专家负责内容真实性，HR 负责方法一致性，法务/合规审查高影响用途。",
    },
    {
      title: "评分治理",
      body:
        "所有评分者必须完成样例校准。对晋升、选拔、调薪等高影响场景，保留评分证据、评分者、版本和复核记录。",
    },
    {
      title: "数据治理",
      body:
        "区分发展数据和决策数据。360 反馈、教练记录等高敏感材料默认用于个人发展，进入组织分析前应聚合、脱敏并限制访问。",
    },
    {
      title: "更新治理",
      body:
        "每 6-12 个月复查岗位任务和技能库；当流程自动化、法规变化、产品线调整或客户承诺改变时，立即触发专项更新。",
    },
  ],
  sources: [
    {
      title: "SkillsFuture Singapore, Skills Frameworks FAQ",
      url: "https://www.skillsfuture.gov.sg/skills-framework/skills-frameworks-faq",
      accessed,
    },
    {
      title: "U.S. Office of Personnel Management, Job Analysis",
      url: "https://www.opm.gov/policy-data-oversight/assessment-and-selection/job-analysis/",
      accessed,
    },
    {
      title: "U.S. Office of Personnel Management, Competencies",
      url: "https://www.opm.gov/policy-data-oversight/assessment-and-selection/competencies/",
      accessed,
    },
    {
      title: "U.S. Office of Personnel Management, Work Samples and Simulations",
      url: "https://www.opm.gov/policy-data-oversight/assessment-and-selection/other-assessment-methods/work-samples-and-simulations/",
      accessed,
    },
    {
      title: "O*NET Resource Center, The O*NET Content Model",
      url: "https://www.onetcenter.org/content.html",
      accessed,
    },
    {
      title: "SFIA Foundation, SFIA 9 Levels of Responsibility",
      url: "https://sfia-online.org/en/sfia-9/responsibilities",
      accessed,
    },
    {
      title: "Center for Creative Leadership, Skillscope 360 Assessment",
      url: "https://www.ccl.org/leadership-solutions/leadership-development-tools/leadership-assessments/skillscope-360-assessment/",
      accessed,
    },
  ] satisfies WsaSource[],
};

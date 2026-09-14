export const leadershipSelfAssessmentArticle = {
  contents: [
    { id: 'lsa-foundation', label: '评估原则' },
    { id: 'lsa-competencies', label: '能力模型' },
    { id: 'lsa-scale', label: '行为量规' },
    { id: 'lsa-evidence', label: '证据账本' },
    { id: 'lsa-calibration', label: '外部校准' },
    { id: 'lsa-profile', label: '示例画像' },
    { id: 'lsa-action', label: '行动计划' },
    { id: 'lsa-rollout', label: '组织应用' },
  ],
  principles: [
    {
      title: '先定义能力，再进行评分',
      detail: '围绕组织真正需要的领导行为建立共同语言，避免用“有领导力”之类的模糊印象替代判断。',
    },
    {
      title: '分数必须连接工作证据',
      detail: '每项判断都写明情境、行为、结果与反馈来源；没有足够证据时标记“待观察”，而不是猜测。',
    },
    {
      title: '自评是起点，不是结论',
      detail: '将自我认知与同事、经理、团队成员和绩效事实进行校准，重点理解差异背后的具体情境。',
    },
  ],
  competencies: [
    {
      name: '沟通',
      signal: '让方向、取舍和责任对不同对象都清晰可执行。',
      evidence: '会议纪要、关键沟通反馈、信息返工率',
      prompt: '最近一次复杂信息被团队准确执行时，我具体做了什么？',
    },
    {
      name: '决策',
      signal: '在信息不完整时明确标准、权衡风险并及时承担决定。',
      evidence: '决策记录、结果复盘、升级与延误数据',
      prompt: '我是否解释了取舍依据，并为决定设置了复查节点？',
    },
    {
      name: '情绪智力',
      signal: '识别自己与他人的情绪线索，稳定回应并保持关系质量。',
      evidence: '冲突复盘、团队反馈、压力情境观察',
      prompt: '压力上升时，我的行为给团队带来了什么可观察影响？',
    },
    {
      name: '战略思考',
      signal: '把外部变化、长期价值与当前资源选择连接起来。',
      evidence: '方案假设、资源取舍、风险与机会清单',
      prompt: '这项短期决定如何支持未来一至两年的组织能力？',
    },
    {
      name: '授权',
      signal: '明确成果边界、决策权限和检查节奏，而不是只分配任务。',
      evidence: '责任矩阵、升级次数、团队独立决策案例',
      prompt: '我保留了哪些本可交给团队的决定，原因是什么？',
    },
    {
      name: '发展他人',
      signal: '通过反馈、挑战任务与持续支持帮助他人形成新能力。',
      evidence: '发展计划、反馈记录、能力前后变化',
      prompt: '过去一个季度，谁因为我的支持能够独立完成更难的工作？',
    },
    {
      name: '自我管理',
      signal: '稳定管理优先级、承诺、精力与学习节奏。',
      evidence: '承诺兑现率、优先级记录、个人复盘',
      prompt: '哪些重复性行为正在削弱我的可靠性或注意力？',
    },
  ],
  scale: [
    {
      level: '1',
      label: '明显发展需要',
      anchor: '即使在常见情境中也很少表现目标行为，需要持续指导。',
      evidence: '存在重复失误、回避或负面影响的具体案例。',
    },
    {
      level: '2',
      label: '正在建立',
      anchor: '在低复杂度情境中偶尔做到，但表现不稳定，仍依赖提醒。',
      evidence: '至少一个有效案例，同时存在相反案例。',
    },
    {
      level: '3',
      label: '稳定胜任',
      anchor: '在日常及部分困难情境中能独立表现目标行为。',
      evidence: '多个近期案例，且结果与他人反馈大体一致。',
    },
    {
      level: '4',
      label: '显著优势',
      anchor: '在复杂情境中持续有效，并能帮助团队提高表现。',
      evidence: '跨情境成果、利益相关者反馈及可复用做法。',
    },
    {
      level: '5',
      label: '组织级示范',
      anchor: '在高不确定性中塑造标准、辅导他人并影响更大范围。',
      evidence: '组织层影响、持续成果和他人能力提升证据。',
    },
  ],
  evidenceLedger: [
    {
      competency: '授权',
      context: '跨部门产品上线',
      behavior: '明确三类决策权限，并把周会改为风险检查。',
      result: '团队自行解决大多数日常取舍，升级事项减少。',
      confidence: '高',
    },
    {
      competency: '沟通',
      context: '季度战略调整',
      behavior: '只发布结论，未说明停止事项与判断标准。',
      result: '两个团队继续投入已降级项目，产生返工。',
      confidence: '高',
    },
    {
      competency: '发展他人',
      context: '新经理培养',
      behavior: '安排挑战任务，但反馈节奏不固定。',
      result: '能力有所提升，关键节点仍依赖临时支持。',
      confidence: '中',
    },
  ],
  reflectionPrompts: [
    '哪项优势已经被结果和不同来源的反馈反复验证？',
    '哪项高分缺少近期证据，可能只是我对自己的期待？',
    '哪些低分来自暂时缺少机会，而不一定代表能力不足？',
    '团队成员最可能指出哪个盲点？我愿意向谁求证？',
    '如果只改变一个行为，哪项会对业务和团队产生最大影响？',
  ],
  calibrationSteps: [
    {
      step: '01',
      title: '选择校准者',
      detail: '邀请一位熟悉工作表现且愿意直言的经理、同事、导师或团队成员。',
    },
    {
      step: '02',
      title: '共享证据而非结论',
      detail: '先展示关键事件与行为记录，再比较双方评分，减少对数字本身的争辩。',
    },
    {
      step: '03',
      title: '定位差异',
      detail: '区分信息缺口、情境差异、标准理解差异和真实盲点。',
    },
    {
      step: '04',
      title: '约定观察',
      detail: '为仍不确定的能力设置未来四周可观察行为和反馈节点。',
    },
  ],
  biasChecks: [
    { name: '近因偏差', correction: '至少回看一个完整季度，并同时记录正反案例。' },
    { name: '光环效应', correction: '逐项按行为证据评分，不让一项强项代表全部能力。' },
    { name: '意图替代影响', correction: '分别记录“我想做什么”和“他人实际经历了什么”。' },
    { name: '严宽不一', correction: '先阅读同一行为量规，再开始个人或群体评分。' },
    { name: '身份标签', correction: '不以性格、资历或职位推断能力，只讨论工作行为。' },
  ],
  profile: [
    { competency: '沟通', self: 4.2, calibrated: 3.4, status: '需要校准' },
    { competency: '决策', self: 3.5, calibrated: 3.7, status: '认知一致' },
    { competency: '情绪智力', self: 3.1, calibrated: 3.8, status: '被低估优势' },
    { competency: '战略思考', self: 4.0, calibrated: 4.1, status: '稳定优势' },
    { competency: '授权', self: 3.8, calibrated: 2.9, status: '优先盲点' },
    { competency: '发展他人', self: 2.8, calibrated: 3.0, status: '发展重点' },
    { competency: '自我管理', self: 3.6, calibrated: 3.5, status: '认知一致' },
  ],
  actionPlan: [
    {
      phase: 'Goal',
      question: '希望形成什么可观察变化？',
      example: '八周内让两名项目负责人独立完成日常资源取舍。',
    },
    {
      phase: 'Reality',
      question: '当前行为和证据说明什么？',
      example: '我频繁介入细节，团队遇到风险时首先等待批准。',
    },
    {
      phase: 'Options',
      question: '有哪些练习、支持与情境？',
      example: '明确决策边界、影子辅导、会后反馈和双周复盘。',
    },
    {
      phase: 'Way forward',
      question: '下一步由谁在何时完成？',
      example: '本周发布权限清单；第 2、4、8 周检查升级率与团队反馈。',
    },
  ],
  rollout: [
    {
      title: '统一框架',
      detail: '确定与战略和领导者层级匹配的能力模型、行为定义和证据口径。',
    },
    {
      title: '小范围试点',
      detail: '先由一组经理完成自评、校准和行动计划，验证理解与体验。',
    },
    {
      title: '保护用途',
      detail: '明确数据用于发展，不直接作为薪酬或晋升的单一判断依据。',
    },
    {
      title: '汇总主题',
      detail: '只在足够群体规模下查看匿名聚合趋势，用于设计共同发展投入。',
    },
    {
      title: '追踪变化',
      detail: '在 60 至 90 天后回看行为证据，而不是只比较一次性分数。',
    },
  ],
  sources: [
    {
      name: 'Mindtools — Leadership Self-Assessment: A Structured Approach',
      url: 'https://www.mindtools.com/your-toolkit/feedback-and-assessment/leadership-self-assessment/',
      accessedAt: '2026-08-11',
      note: '用于核实能力框架、五点行为量规、开放反思、外部验证、行动转化与组织聚合应用原则。',
    },
  ],
} as const;

export type FeedbackRaterGroup = {
  id: string;
  label: string;
  perspective: string;
  target: string;
  reportingRule: string;
};

export type FeedbackProcessStep = {
  number: string;
  title: string;
  purpose: string;
  actions: readonly string[];
  output: string;
};

export const threeSixtyFeedbackContent = {
  source: {
    title: 'SurveyMonkey · 360-Degree Feedback Surveys: Template and Best Practices',
    url: 'https://www.surveymonkey.com/learn/employee-feedback/360-employee-feedback-survey-example/',
    accessedAt: '2026-08-11',
    note: '本页依据公开方法要点进行中文场景化重写，不包含原站问卷、模板或评分工具。',
  },
  intro: {
    eyebrow: 'Feedback system',
    title: '把多方观察转化为可执行的行为改变',
    lead: '360 度反馈汇集本人、经理、同事与直接下属对日常行为的观察，用来识别稳定优势、自我认知差异和少量高价值发展重点。',
    principles: [
      '用于发展与教练，不直接绑定薪酬、晋升或绩效定级。',
      '讨论重复出现的模式，不追查单条匿名意见来自谁。',
      '报告只是起点，60–90 天后的行为证据才是结果。',
    ],
  },
  contents: [
    { id: 'f360-coverage', label: '视角覆盖' },
    { id: 'f360-blueprint', label: '题项蓝图' },
    { id: 'f360-process', label: '六步实施' },
    { id: 'f360-compare', label: '自他差异' },
    { id: 'f360-debrief', label: '反馈会谈' },
    { id: 'f360-action', label: '行动与跟进' },
    { id: 'f360-safeguards', label: '治理护栏' },
  ],
  raterGroups: [
    {
      id: 'self',
      label: '本人',
      perspective: '记录意图、自我判断与希望强化的行为。',
      target: '1 人',
      reportingRule: '单列展示，用于与他评比较，不作匿名处理。',
    },
    {
      id: 'manager',
      label: '经理',
      perspective: '观察目标、决策、交付和跨团队影响。',
      target: '1–2 人',
      reportingRule: '可单列，但应事先说明身份可被识别。',
    },
    {
      id: 'peers',
      label: '同事',
      perspective: '观察协作、信息共享、信任与冲突处理。',
      target: '3–5 人',
      reportingRule: '通常至少 3 份有效答卷后才单独汇总。',
    },
    {
      id: 'reports',
      label: '直接下属',
      perspective: '观察授权、反馈、倾听与团队环境。',
      target: '3–5 人',
      reportingRule: '不足 3 人时合并或隐藏，避免反向识别。',
    },
    {
      id: 'partners',
      label: '合作方',
      perspective: '补充客户、项目伙伴或关键接口的体验。',
      target: '按场景选取',
      reportingRule: '仅选择能持续观察目标行为的人。',
    },
  ] satisfies readonly FeedbackRaterGroup[],
  scale: {
    title: '使用一致的行为频率量表',
    description: '5 点或 7 点量表均可，关键是所有题项使用同一方向，并提供“无法观察”选项，避免评价者被迫猜测。',
    points: [
      { value: '1', label: '几乎从不' },
      { value: '2', label: '较少出现' },
      { value: '3', label: '有时出现' },
      { value: '4', label: '经常出现' },
      { value: '5', label: '持续表现' },
      { value: 'N/A', label: '无法观察' },
    ],
  },
  questionBlueprint: [
    {
      competency: '沟通与倾听',
      behavior: '在讨论开始前说明目标、背景和需要做出的决定。',
      avoid: '沟通能力很强。',
      followUp: '请描述一次这项行为帮助或阻碍团队推进的情境。',
    },
    {
      competency: '协作与信任',
      behavior: '在跨团队工作中主动共享影响他人的变化与风险。',
      avoid: '是一位优秀的团队成员。',
      followUp: '哪一种具体做法值得继续？哪一种需要调整？',
    },
    {
      competency: '发展他人',
      behavior: '反馈时同时说明观察到的行为、影响和下一步期待。',
      avoid: '很会培养员工。',
      followUp: '什么支持会让这项行为更有效？',
    },
    {
      competency: '执行与担当',
      behavior: '承诺发生变化时及时重排优先级并告知相关人员。',
      avoid: '责任心很强。',
      followUp: '请提供一个可验证的工作例子。',
    },
  ],
  process: [
    {
      number: '01',
      title: '设计题项',
      purpose: '围绕角色关键能力，只测量日常能够观察的单一行为。',
      actions: ['每个题项只包含一个动作', '统一使用 5 点或 7 点频率量表', '配置 2–3 个开放问题补充情境'],
      output: '行为题库与评分说明',
    },
    {
      number: '02',
      title: '确定保密规则',
      purpose: '在邀请发出前说明谁能看见什么，以及小样本如何处理。',
      actions: ['同事与下属通常至少 3 人才单列', '按角色聚合分数', '删除可识别个人的敏感细节'],
      output: '匿名阈值与数据权限表',
    },
    {
      number: '03',
      title: '培训参与者',
      purpose: '让评价者和被评价者理解发展用途、量表和报告形式。',
      actions: ['展示一个题项和一页模拟报告', '练习基于行为的反馈', '明确不与薪酬或晋升直接绑定'],
      output: '参与者说明与示例',
    },
    {
      number: '04',
      title: '收集反馈',
      purpose: '在明确窗口内获得各组具有代表性的有效观察。',
      actions: ['公布开放与截止日期', '监控各评价组完成率', '只向未完成组发送克制提醒'],
      output: '达到匿名阈值的数据集',
    },
    {
      number: '05',
      title: '分析数据',
      purpose: '从分数与评论中识别稳定模式、自他差异和优先主题。',
      actions: ['先看能力整体趋势', '再比较本人和各评价组', '用评论验证数字背后的情境'],
      output: '优势、盲点与发展主题',
    },
    {
      number: '06',
      title: '反馈与发展计划',
      purpose: '通过会谈消化结果，并把洞察转化为少量行为实验。',
      actions: ['聚焦 3–5 项优势与 2–3 项重点', '约定练习场景和支持人', '安排 60–90 天跟进'],
      output: '可观察的行动计划',
    },
  ] satisfies readonly FeedbackProcessStep[],
  comparison: [
    { behavior: '会前说明决策边界', self: '4.5', others: '3.2', gap: '+1.3', reading: '可能高估信息清晰度，先核对具体会议情境。' },
    { behavior: '邀请不同意见', self: '3.1', others: '4.2', gap: '-1.1', reading: '他人已感受到优势，可继续保持并识别有效做法。' },
    { behavior: '承诺变化时及时同步', self: '3.7', others: '3.6', gap: '+0.1', reading: '自他看法一致，结合评论判断稳定性。' },
  ],
  reportGuide: [
    { title: '先找重复信号', detail: '多个评价组都观察到的模式，通常比单个极端分数更值得关注。' },
    { title: '再看自他差异', detail: '差异是探索入口，不等于本人错误或他人正确。' },
    { title: '把评论放回情境', detail: '确认行为出现在哪些任务、关系和压力条件中。' },
    { title: '控制发展重点', detail: '优先选择一到两个同时影响业务结果与团队体验的行为。' },
  ],
  debrief: [
    { phase: '承接', prompt: '看到结果后，你最强烈的感受是什么？先不急着解释。' },
    { phase: '理解', prompt: '哪些模式与你的经验一致？哪些信息让你意外？' },
    { phase: '核证', prompt: '评论中有哪些可验证的情境、行为和影响？' },
    { phase: '选择', prompt: '如果只调整一个行为，哪一项最能改善当前工作？' },
    { phase: '承诺', prompt: '下次在哪个真实场景练习？谁可以提供即时反馈？' },
  ],
  actionPlan: [
    { horizon: '本周', action: '选择一个发展行为并定义触发场景。', evidence: '写出“何时、做什么、他人能看到什么”。', support: '与经理或教练校准。' },
    { horizon: '第 2–4 周', action: '在至少三个真实场景中练习。', evidence: '记录行为、结果和一条即时反馈。', support: '邀请一名同事作为观察伙伴。' },
    { horizon: '第 30 天', action: '复盘有效做法与阻碍。', evidence: '比较三次实践中的共同模式。', support: '调整练习难度或环境支持。' },
    { horizon: '第 60–90 天', action: '完成定向脉冲反馈。', evidence: '用 2–3 个相同题项检查行为频率变化。', support: '决定保持、升级或更换发展重点。' },
  ],
  safeguards: [
    { title: '用途隔离', detail: '发展性 360 与薪酬、晋升评定分开运行，降低策略性评分。' },
    { title: '最小样本', detail: '每个匿名评价组通常至少 3 人；不足时合并或不展示。' },
    { title: '知情与权限', detail: '提前说明数据用途、可见范围、保存周期和删除规则。' },
    { title: '代表性选择', detail: '评价者应有足够合作频率，并覆盖不同关系和工作场景。' },
    { title: '专业会谈', detail: '避免把未经解释的报告直接交给参与者独自消化。' },
    { title: '持续跟进', detail: '60–90 天检查行为证据，不把一次测评当成永久标签。' },
  ],
} as const;

export type ThreeSixtyFeedbackContent = typeof threeSixtyFeedbackContent;

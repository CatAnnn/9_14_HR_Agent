export type BelbinRoleGroupId = 'social' | 'thinking' | 'action';

export interface BelbinTeamRole {
  id: string;
  name: string;
  englishName: string;
  contribution: string;
  strength: string;
  allowableRisk: string;
  teamQuestion: string;
}

export interface BelbinRoleGroup {
  id: BelbinRoleGroupId;
  label: string;
  englishLabel: string;
  summary: string;
  roles: readonly BelbinTeamRole[];
}

export const belbinArticleNav = [
  { id: 'belbin-foundation', label: '先理解边界' },
  { id: 'belbin-role-explorer', label: '九类贡献' },
  { id: 'belbin-coverage', label: '覆盖规划' },
  { id: 'belbin-project', label: '项目阶段' },
  { id: 'belbin-collaboration', label: '协作组合' },
  { id: 'belbin-workshop', label: '团队工作坊' },
  { id: 'belbin-guardrails', label: '使用边界' },
] as const;

export const belbinRoleGroups: readonly BelbinRoleGroup[] = [
  {
    id: 'social',
    label: '关系与协同',
    englishLabel: 'Social roles',
    summary: '连接外部机会、维护团队合作，并把共同目标转化为清晰分工。',
    roles: [
      {
        id: 'resource-investigator',
        name: '资源探索者',
        englishName: 'Resource Investigator',
        contribution: '主动向外寻找信息、机会与合作关系，把新线索带回团队。',
        strength: '好奇、热情，善于建立联系并打开新的可能性。',
        allowableRisk: '新鲜感下降后可能减少跟进，或对机会过度乐观。',
        teamQuestion: '哪些外部线索需要明确负责人继续验证和跟进？',
      },
      {
        id: 'teamworker',
        name: '团队协作者',
        englishName: 'Teamworker',
        contribution: '倾听不同意见、缓和摩擦，并在团队需要时补位。',
        strength: '敏锐、合作、善于体察他人并维护有效关系。',
        allowableRisk: '高压决策时可能犹豫，或为了和谐回避必要冲突。',
        teamQuestion: '怎样让分歧被坦诚讨论，同时不伤害合作关系？',
      },
      {
        id: 'co-ordinator',
        name: '协调者',
        englishName: 'Co-ordinator',
        contribution: '澄清团队目标，识别成员优势，并把责任交给合适的人。',
        strength: '成熟、稳定，能够聚焦目标并调动不同人才。',
        allowableRisk: '过度授权时可能让人感到在转移自己的工作。',
        teamQuestion: '目标、决策权和交付责任是否已经说清楚？',
      },
    ],
  },
  {
    id: 'thinking',
    label: '思考与判断',
    englishLabel: 'Thinking roles',
    summary: '提出原创方案、审慎比较选择，并为关键问题提供专业深度。',
    roles: [
      {
        id: 'plant',
        name: '创意者',
        englishName: 'Plant',
        contribution: '以非传统视角提出新想法，处理模糊或棘手的问题。',
        strength: '富有想象力，擅长生成突破性方案。',
        allowableRisk: '专注大想法时可能忽略细节，也可能没有充分解释思路。',
        teamQuestion: '创意需要哪些约束、数据和后续验证才能落地？',
      },
      {
        id: 'monitor-evaluator',
        name: '审慎评估者',
        englishName: 'Monitor Evaluator',
        contribution: '冷静比较备选方案，识别逻辑漏洞并判断可行性。',
        strength: '客观、审慎，能够在复杂选择中保持判断质量。',
        allowableRisk: '可能显得过于挑剔，或因为持续分析而放慢决定。',
        teamQuestion: '我们需要什么证据来结束分析并做出决定？',
      },
      {
        id: 'specialist',
        name: '专业者',
        englishName: 'Specialist',
        contribution: '为关键领域提供深度知识、方法与专业标准。',
        strength: '专注、自驱，能够解决高度专业化的问题。',
        allowableRisk: '视角可能集中在较窄领域，沟通时容易陷入技术细节。',
        teamQuestion: '哪些专业判断必须转译成全队都能使用的结论？',
      },
    ],
  },
  {
    id: 'action',
    label: '行动与交付',
    englishLabel: 'Task / Action roles',
    summary: '保持推进压力，把方案转化为计划，并在交付前守住质量。',
    roles: [
      {
        id: 'shaper',
        name: '推进者',
        englishName: 'Shaper',
        contribution: '在压力与障碍下保持节奏，推动团队正面处理难题。',
        strength: '有驱动力、敢于挑战，能避免团队失去焦点。',
        allowableRisk: '推进过猛时可能让他人感到被冒犯或被压迫。',
        teamQuestion: '怎样既保持紧迫感，又为不同意见留出空间？',
      },
      {
        id: 'implementer',
        name: '执行者',
        englishName: 'Implementer',
        contribution: '把构想整理成现实可行的步骤、流程和责任安排。',
        strength: '务实、可靠，能够稳定组织工作并持续执行。',
        allowableRisk: '既有计划受到挑战时可能不够灵活，错过新的可能。',
        teamQuestion: '哪些流程应当稳定执行，哪些假设需要定期重看？',
      },
      {
        id: 'completer-finisher',
        name: '完成者',
        englishName: 'Completer Finisher',
        contribution: '在交付前检查遗漏、错误和质量细节，推动真正收尾。',
        strength: '认真、尽责，对截止时间和完成质量保持敏感。',
        allowableRisk: '可能过度担忧细节，或因为不放心而不愿授权。',
        teamQuestion: '哪些质量标准是必须项，哪些细节可以适度取舍？',
      },
    ],
  },
] as const;

export const belbinProjectStages = [
  {
    stage: '探索机会',
    need: '打开信息边界，形成多个可能方向。',
    contributions: '资源探索者、创意者、专业者',
    check: '先扩展选择，再约定验证标准，不把最早出现的想法当结论。',
  },
  {
    stage: '选择方向',
    need: '比较证据、澄清目标并形成承诺。',
    contributions: '审慎评估者、协调者、团队协作者',
    check: '把分歧和取舍写清楚，同时确认谁拥有最终决定权。',
  },
  {
    stage: '规划实施',
    need: '把方向转化为步骤、资源和依赖关系。',
    contributions: '执行者、协调者、专业者',
    check: '标明责任、截止时间、质量标准以及需要重新评估的假设。',
  },
  {
    stage: '推进交付',
    need: '保持节奏、解除障碍并确保团队协作。',
    contributions: '推进者、执行者、团队协作者',
    check: '同时观察任务进度和协作负荷，避免速度以关系透支为代价。',
  },
  {
    stage: '检查收尾',
    need: '验证质量、关闭缺口并沉淀学习。',
    contributions: '完成者、审慎评估者、团队协作者',
    check: '区分关键缺陷与可接受差异，完成交付后再进行复盘。',
  },
] as const;

export const belbinCollaborationPairs = [
  {
    title: '创意者 × 审慎评估者',
    value: '一个扩展可能性，一个检验证据与约束。',
    agreement: '先约定发散时间，再用公开标准筛选，不在创意刚出现时立即否定。',
  },
  {
    title: '资源探索者 × 执行者',
    value: '一个发现外部机会，一个把机会变成可跟进的行动。',
    agreement: '每条新线索都明确验证动作、负责人和关闭条件。',
  },
  {
    title: '推进者 × 团队协作者',
    value: '一个保持速度，一个观察关系与团队承载力。',
    agreement: '允许直接挑战任务，但不把压力转移为对人的攻击。',
  },
  {
    title: '协调者 × 专业者',
    value: '一个保持全局和分工，一个守住关键专业判断。',
    agreement: '专业结论需说明业务影响；协调者不能用进度替代专业标准。',
  },
  {
    title: '执行者 × 完成者',
    value: '一个建立稳定流程，一个在交付前发现遗漏。',
    agreement: '提前定义完成标准，避免在最后阶段无限增加检查范围。',
  },
] as const;

export const belbinExampleTeam = {
  title: '示例：跨部门上线一个经理反馈工具',
  context: '这是团队贡献讨论示例，不是对成员进行 Belbin 角色判定。',
  observations: [
    '早期讨论有大量业务想法，但没有人持续验证用户场景和外部依赖。',
    '项目计划清晰、推进速度快，却在评审时反复出现可用性细节遗漏。',
    '专业意见充分，但部分术语没有转化为产品、法务和 HR 可以共同决策的语言。',
  ],
  actions: [
    '为外部访谈和依赖验证设置明确负责人，每周关闭未验证假设。',
    '在每个里程碑加入完成标准检查，而不是等上线前集中查漏。',
    '让专业负责人以“结论、影响、建议”三句话转译关键判断。',
  ],
} as const;

export const belbinWorkshopSteps = [
  {
    title: '从目标开始',
    detail: '写下未来 6 至 12 周最重要的团队成果和关键风险，而不是先讨论谁属于哪个角色。',
  },
  {
    title: '列出所需贡献',
    detail: '按探索、判断、协同、执行和收尾识别工作需要，允许同一成员在不同情境提供不同贡献。',
  },
  {
    title: '寻找行为证据',
    detail: '使用近期会议、决策和交付实例讨论已经出现或持续缺失的行为，避免凭印象贴标签。',
  },
  {
    title: '处理缺口与重叠',
    detail: '缺口可以通过分工、协作、外部支持或流程补位；重叠则需要明确谁主责、谁挑战。',
  },
  {
    title: '形成工作约定',
    detail: '每项约定包含场景、行为、责任人和复盘日期，两到四周后根据实际效果调整。',
  },
] as const;

export const belbinSafeguards = [
  {
    title: '不要把行为变成固定身份',
    detail: '团队角色描述的是情境中的贡献模式，不是人格类型。岗位、同事、压力和环境变化时，行为也可能变化。',
  },
  {
    title: '不要用本页替代官方评估',
    detail: '本页没有问卷、计分、常模或报告功能。需要个人或团队正式报告时，应使用 Belbin 官方授权工具和合格支持。',
  },
  {
    title: '不要把“可容许弱点”当免责条款',
    detail: '只有当相应优势确实为团队创造价值、且风险没有伤害绩效与关系时，才可讨论如何管理这种取舍。',
  },
  {
    title: '不要替代岗位能力与绩效证据',
    detail: '贡献偏好不能证明专业胜任、工作结果或晋升资格，必须与任务、能力和绩效事实分开使用。',
  },
  {
    title: '不要强迫公开个人标签',
    detail: '团队讨论应聚焦当前工作需要和可观察行为，不要求成员接受单一称谓，也不用于招聘筛选或奖惩。',
  },
] as const;

export const belbinSource = {
  title: 'Belbin Team Roles',
  organization: 'Belbin',
  url: 'https://www.belbin.com/about/belbin-team-roles',
  accessedAt: '2026-08-11',
  note: '参考官方对九类团队角色、三组分类、优势与可容许弱点，以及行为会随工作情境变化的说明。正文均为中文场景化改写。',
  copyright:
    'Belbin 官方说明不存在获授权的免费自评分测试，Self-Perception Inventory、问卷、计分网格、报告与相关图示受版权保护。本页不复制或替代这些材料。',
} as const;

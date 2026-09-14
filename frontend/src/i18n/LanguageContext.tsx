import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react';
import {
  formatTranslationTemplate,
  normalizeAppLanguage,
  setCurrentLanguage,
  SUPPORTED_LANGUAGES,
  type AppLanguage,
  type TranslationTemplateValues,
} from './languageRuntime';
import { createLanguageChangeHandler, languageCatalogs } from './languageCatalogs';

export type { AppLanguage } from './languageRuntime';
export {
  getCurrentLanguage,
  languageName,
  normalizeAppLanguage,
  SUPPORTED_LANGUAGES,
} from './languageRuntime';

const LANGUAGE_STORAGE_KEY = 'hr-agent-language';

export const ENGLISH_TEXT: Readonly<Record<string, string>> = {
  '开场定调': 'Opening alignment',
  '情绪承接': 'Emotional acknowledgment',
  '发展计划': 'Development plan',
  '绩效管理': 'Performance management',
  '绩效管理的五个维度': 'Five dimensions of performance management',
  '解决方案': 'Solutions',
  '资源中心': 'Resources',
  '绩效管理维度': 'Performance management dimension',
  '延伸阅读': 'Further reading',
  '从理解绩效、设定目标、反馈评估，到结果管理与低绩效改进，按五个维度找到当前需要的方法与行动提示。':
    'Explore methods and action guidance across five dimensions, from understanding performance and setting goals to feedback, evaluation, results, and low-performance management.',
  '从评估者到赋能者': 'From evaluator to enabler',
  '让绩效管理成为持续发生的管理对话':
    'Make performance management an ongoing management conversation',
  '五个维度沿用绩效管理草稿的原始章节，不增加新的分类；每一页都将长段落转化为对比、步骤、检查项和行动提示。':
    'The five dimensions follow the original performance-management chapters without adding new categories. Each page turns long passages into comparisons, steps, checklists, and action prompts.',
  '涉及奖金、PIP、合同处理等尚未定稿的政策内容会明确提示审核状态，并以正式政策及 HRBP/Legal 确认为准。':
    'Draft policy content involving bonuses, PIPs, or contract actions is clearly marked for review and remains subject to formal policy and HRBP or Legal confirmation.',
  '按顺序阅读完整路径，或直接进入当前管理任务对应的维度。':
    'Follow the complete path in order, or go directly to the dimension relevant to your current management task.',
  '把方法转化为下一次管理行动': 'Turn methods into your next management action',
  '完成阅读后，可进入沟通工作台准备谈话、整理事实并开展练习。':
    'After reading, open the conversation workspace to prepare, organize the facts, and rehearse.',
  '书籍': 'Books',
  '从战略分析、教练目标、反馈评估到发展框架，形成可直接应用的人才工具库。':
    'A practical talent toolkit spanning strategy, coaching, feedback, assessment, and development.',
  '阅读沟通、谈判、影响力与个人发展的精选书籍。':
    'Explore selected books on communication, negotiation, influence, and personal development.',
  '让每一次绩效反馈，成为真实成长的起点':
    'Make Every Performance Conversation the Start of Real Growth.',
  '让每一次绩效反馈，': 'Every Feedback',
  '成为真实成长的起点': 'Starts Real Growth',
  '55 年专注于领导力：帮企业选得准、育得快、升得对':
    '55 years of leadership expertise to help organizations select, develop, and promote the right people.',
  '阳光下的山峰、山谷与高山草甸': 'Sunlit mountains, valleys, and alpine meadows',
  '团队围绕战略与业务问题开展结构化分析':
    'A team conducting structured analysis of strategy and business challenges',
  '经理与员工围绕目标开展教练对话':
    'A manager and employee holding a coaching conversation about goals',
  '基于反馈与评估数据讨论人才发展':
    'A talent development discussion grounded in feedback and assessment data',
  '围绕职业和管理能力构建发展框架':
    'Building a development framework around career and management capabilities',
  '关于 Performance Feedback': 'About Performance Feedback',
  '全球领先的人才评鉴与领导力发展专家':
    'A global leader in talent assessment and leadership development',
  '关于人才，只有真正理解人的成长规律，才能帮助组织在变化中持续前行。':
    'Organizations move forward through change when they understand how people grow.',
  '人才与领导力发展品牌视觉': 'Talent and leadership development',
  '一体化解决方案': 'Integrated solutions',
  '选择适合当前议题的解决方案': 'Choose the right solution for your current challenge',
  '全部解决方案': 'All solutions',
  '查看解决方案': 'View solution',
  '联系我们': 'Contact us',
  '连接人才洞察、专业资源与真实管理实践。':
    'Connecting talent insight, professional resources, and real management practice.',
  '关于我们': 'About',
  '平台介绍': 'Overview',
  '人才咨询': 'Talent consulting',
  '测评诊断': 'Assessment',
  '发展培养': 'Development',
  'HR 赋能': 'HR enablement',
  '仅限授权用户使用': 'Authorized users only',
  '更有准备地完成每一次关键人才对话':
    'Prepare for every critical talent conversation',
  '围绕员工背景与沟通目标，获得谈前指导、沉浸式多轮预演和四维复盘，将管理判断转化为清晰表达与后续行动。':
    'Use employee context and conversation goals to receive preparation guidance, immersive rehearsal, and four-dimensional review that turn management judgment into clear communication and action.',
  '正在进入': 'Opening',
  '进入 Agent': 'Open Agent',
  '经理与员工围绕工作表现和发展目标展开沟通':
    'A manager and employee discussing performance and development goals',
  '探索全部': 'Explore all',
  '返回首页顶部': 'Back to the top',
  '返回首页': 'Back to home',
  '首页主导航': 'Main navigation',
  '关闭导航': 'Close navigation',
  '打开导航': 'Open navigation',
  '解决方案轮播': 'Solutions carousel',
  '轮播': 'carousel',
  '管理沟通解决方案': 'Management conversation solutions',
  '将经过验证的方法与模型带入人才管理场景，帮助管理者看清问题、形成判断，并将每一次对话转化为可执行的行动。':
    'Bring proven methods and models into talent management to clarify issues, strengthen judgment, and turn every conversation into action.',
  '从洞察到行动': 'From insight to action',
  '成熟框架，让复杂判断更清晰': 'Proven frameworks for clearer complex decisions',
  '优秀的管理决策很少依赖直觉本身。成熟的方法能够帮助管理者整理信息、检验假设，并在复杂的人才议题中聚焦真正影响结果的因素。':
    'Strong management decisions rarely rely on intuition alone. Proven methods organize information, test assumptions, and focus attention on what truly affects outcomes.',
  '本页汇集战略分析、目标设定、反馈评估与人才发展的常用框架。无需一次掌握全部内容，从当前最需要解决的问题开始，选择一个方法并在真实沟通中持续应用。':
    'Explore practical frameworks for strategy, goals, feedback, assessment, and talent development. Start with the issue that matters now and apply one method in real conversations.',
  '具体提供方式': 'What we provide',
  '以四类经过实践验证的框架，将复杂判断转化为清晰、可执行的下一步行动。':
    'Four practice-tested framework families turn complex judgment into clear next steps.',
  '应用到真实场景': 'Apply it in practice',
  '将方法带入下一次管理对话': 'Bring the method into your next management conversation',
  '结合员工档案、沟通意图和多轮预演，把专业框架转化为可执行的沟通行动。':
    'Combine employee context, conversation intent, and rehearsal to turn professional frameworks into executable communication.',
  '进入沟通工作台': 'Open conversation workspace',
  '进入预演': 'Start rehearsal',
  '连接人才洞察、专业框架与真实管理实践。':
    'Connecting talent insight, professional frameworks, and real management practice.',
  '快速入口': 'Quick links',
  '沟通工作台': 'Conversation workspace',
  '精选书籍': 'Selected books',
  '面向真实管理问题的精选书籍': 'Selected books for real management challenges',
  '从阅读到实践': 'From reading to practice',
  '汇集沟通、谈判、影响力与个人发展领域的精选书籍，帮助管理者系统理解真实沟通问题。':
    'Selected books on communication, negotiation, influence, and personal development help managers understand real conversation challenges.',
  '每本书都提供清晰简介、在线阅读与下载入口，便于在准备对话和制定行动时随时查阅。':
    'Each book includes a clear overview with online reading and EPUB download access.',
  '选择书籍查看简介、在线阅读或下载 EPUB':
    'Choose a book to view its overview, read online, or download the EPUB.',
  '把阅读转化为真实沟通行动': 'Turn reading into real communication action',
  '从书中选择适合当前管理场景的方法，并在沟通工作台中完成准备与演练。':
    'Choose methods that fit the current management situation, then prepare and rehearse them in the conversation workspace.',
  '全部书籍': 'All books',
  '浏览书籍': 'Browse books',
  '书籍详情': 'Book details',
  '首页': 'Home',
  '页面': 'Page',
  '工作平台': 'Workspace',
  '管理工具': 'Management tool',
  '请输入关键词...': 'Search by keyword...',
  '搜索网站内容': 'Search site content',
  '搜索网站': 'Search site',
  '搜索': 'Search',
  '正在加载搜索': 'Loading search',
  '关闭搜索': 'Close search',
  '全站搜索': 'Site search',
  '搜索建议': 'Search suggestions',
  '没有找到匹配内容，请尝试其他关键词。':
    'No matching content was found. Try another keyword.',
  '语言': 'Language',
  '个人资料': 'Profile',
  '设置': 'Settings',
  '管理员': 'Administrator',
  '授权用户': 'Authorized user',
  '管理账号': 'Manage accounts',
  '退出登录': 'Log out',
  '账号与白名单': 'Accounts and allowlist',
  '打开账号菜单': 'Open account menu',
  '账号菜单': 'Account menu',
  '首页页脚导航': 'Home footer navigation',
  '资源页页脚导航': 'Resources footer navigation',
  '解决方案主题': 'Solution topics',
  '资源与工作台': 'Resources and workspace',
  '工作台': 'Workspace',
  '开始准备': 'Start preparing',
  '管理沟通工作台': 'Management conversation workspace',
  '结合员工档案、沟通意图、谈前指导与多轮预演，准备下一次管理对话。':
    'Prepare the next management conversation with employee context, intent, guidance, and rehearsal.',
  '从战略分析、教练目标、反馈评估到发展框架，查找可直接应用的方法。':
    'Find practical methods across strategy, coaching, feedback, assessment, and development.',
  '团队围绕业务与发展议题开展协作讨论':
    'A team collaborating on business and development topics',
  '战略与分析': 'Strategy & Analysis',
  '教练与目标': 'Coaching & Goal',
  '反馈与评估': 'Feedback & Assessment',
  '发展框架': 'Development Framework',
  '发展框架与模型': 'Development frameworks and models',
  '战略与分析：行动前先看清全局':
    'Strategy and analysis: see the full picture before you act',
  '教练与目标：推动真实进展的框架':
    'Coaching and goals: the frameworks behind real progress',
  '反馈与评估：建立自我认知，打造更好的团队':
    'Feedback and assessment: build self-awareness, build better teams',
  '发展框架：理解专业人士与组织如何成长':
    'Development frameworks: understand how professionals and organizations grow',
  '在行动之前看清全局，用结构化分析把分散信息转化为清晰、可解释的决策依据。':
    'See the whole picture before acting, using structured analysis to turn scattered information into clear, explainable decisions.',
  '为目标设定和教练对话建立清晰节奏，让思考、选择与责任真正连接起来。':
    'Create a clear rhythm for goal setting and coaching so reflection, choice, and accountability stay connected.',
  '用结构化反馈和评估建立可靠认知，让发展讨论从印象走向可验证的证据。':
    'Use structured feedback and assessment to move development conversations from impressions to verifiable evidence.',
  '理解职业与管理能力如何成长，识别角色转换中的断点，并设计更匹配的发展路径。':
    'Understand how career and management capabilities grow, identify transition gaps, and design better development paths.',
  '两名工程师在施工现场共同检查工程进展':
    'Two engineers inspecting construction progress together on site',
  '两位专业人士在明亮办公室进行一对一辅导交流':
    'Two professionals having a one-to-one coaching conversation in a bright office',
  '玻璃倒影中的专业人员在开放办公空间协作':
    'Professionals collaborating in an open office through a glass reflection',
  '一组专业人士在发展研讨现场专注聆听':
    'A group of professionals listening attentively during a development workshop',
  '四名设计人员围绕建筑蓝图讨论方案':
    'Four designers discussing a proposal around architectural blueprints',
  '工程师在实验室调试精密电子设备':
    'An engineer adjusting precision electronic equipment in a laboratory',
  '三名工程师在汽车测试设备旁协同讨论':
    'Three engineers discussing work beside automotive test equipment',
  '两名工程师使用仪表检测电子电路板':
    'Two engineers using instruments to test an electronic circuit board',
  '一名员工在一对一交流中专注聆听对方':
    'An employee listening attentively during a one-to-one conversation',
  '工程人员在工作台上精确测量金属构件':
    'An engineering professional precisely measuring a metal component at a workbench',
  '多名行人沿现代建筑廊道向前行走':
    'Several people walking forward through a modern architectural corridor',
  '多位同事围绕同一议题交换不同视角':
    'Several colleagues exchanging different perspectives on the same topic',
  '管理者独自在落地窗前沉思':
    'A manager reflecting alone beside a floor-to-ceiling window',
  '三名工程师在汽车原型设备旁协作分析':
    'Three engineers collaborating beside automotive prototype equipment',
  '一名员工专注倾听同事表达观点':
    'An employee listening attentively as a colleague shares a point of view',
  '两名工程师近距离检查并调整电路板':
    'Two engineers closely inspecting and adjusting a circuit board',
  '一位专业人员沿现代建筑楼梯向上行走':
    'A professional walking up a staircase in modern architecture',
  '两位专业人员共同审阅大型工程规划图':
    'Two professionals reviewing a large engineering plan together',
  '两位工程师在实验室共同操作医疗机器人':
    'Two engineers working with a medical robot in a laboratory',
  '文化与价值观': 'Culture and values',
  '发展对话': 'Development conversations',
  '情绪与沟通': 'Emotion and communication',
  '员工与岗位': 'Employees and roles',
  '反馈方法': 'Feedback methods',
  '通用管理': 'General management',
  '岗位与职级': 'Roles and levels',
  '组织信息': 'Organization information',
  '制度红线': 'Policy boundaries',
  '知识资料': 'Knowledge source',
  '让绩效沟通建立在事实、理解与行动之上。':
    'Ground performance conversations in facts, understanding, and action.',
  '解决方案 | Performance Feedback': 'Solutions | Performance Feedback',
  '资源中心 | Performance Feedback': 'Resources | Performance Feedback',
  '未找到书籍': 'Book not found',
  '未找到这本电子书': 'This ebook could not be found',
  '书目可能已经更新，请返回资源中心重新选择。':
    'The catalog may have changed. Return to Resources and choose another book.',
  '返回资源中心': 'Back to Resources',
  '在线书库': 'Online library',
  '出版社': 'Publisher',
  '出版日期': 'Publication date',
  '主题标签': 'Topics',
  '在线阅读': 'Read online',
  '登录后阅读': 'Sign in to read',
  '下载 EPUB': 'Download EPUB',
  '登录后下载': 'Sign in to download',
  '电子书下载失败。': 'The ebook download failed.',
  '封面与简介可公开浏览，正文阅读和下载需要登录。':
    'The cover and overview are public. Sign in to read or download the full book.',
  '继续阅读': 'Continue reading',
  '更多沟通与发展书籍': 'More books on communication and development',
  '你好，我可以根据现有学习资源回答战略分析、教练辅导、反馈评估和人才发展相关问题。':
    'Hello. I can answer questions about strategy, coaching, feedback, assessment, and talent development using the available learning resources.',
  'HR 资源助手': 'HR Resource Assistant',
  '在线': 'Online',
  '关闭资源助手': 'Close resource assistant',
  '打开资源助手': 'Open resource assistant',
  '关闭': 'Close',
  '参考：': 'Sources: ',
  '正在生成回答': 'Generating answer',
  '暂时无法回答，请稍后重试。': 'Unable to answer right now. Please try again later.',
  '输入你的问题': 'Ask a question',
  '输入资源问题': 'Enter a resource question',
  '发送消息': 'Send message',
  '发送': 'Send',
  '员工信息': 'Employee profile',
  '沟通意图': 'Conversation intent',
  '人格与诉求': 'Persona and motives',
  '谈前指导': 'Preparation guidance',
  '多轮预演': 'Rehearsal',
  '复盘报告': 'Review report',
  '工作台导航': 'Workspace navigation',
  '返回主页': 'Back to home',
  '展开侧边栏': 'Expand sidebar',
  '收起侧边栏': 'Collapse sidebar',
  '流程步骤': 'Workflow steps',
  '工作台载入失败，请重新加载': 'The workspace failed to load. Please reload it.',
  '重新加载': 'Reload',
  '正在载入工作台': 'Loading workspace',
  '正在加载当前步骤': 'Loading this step',
  '切换为英文': 'Switch to English',
  '切换为中文': 'Switch to Chinese',
  '开场定调与绩效结果对齐': 'Opening alignment and performance-result clarity',
  '情绪承接、接纳与共情': 'Emotional acknowledgment, acceptance, and empathy',
  '产出与标准': 'Outcomes and standards',
  '总结与差异化发展计划': 'Summary and differentiated development plan',
  '开场定调与绩效结果对齐评估': 'Opening alignment and performance-result evaluation',
  '情绪承接评估': 'Emotional acknowledgment evaluation',
  '从情绪回归产出与标准评估': 'Outcomes and standards evaluation',
  '总结与差异化发展计划评估': 'Summary and differentiated development-plan evaluation',
  '处理中...': 'Processing...',
  '报告缺少该评估任务。': 'The report is missing this evaluation task.',
  '该评估任务失败。': 'This evaluation task failed.',
  '浏览器草稿存储不可用，本页编辑仍会保留到当前页面关闭。':
    'Browser draft storage is unavailable. Your edits will remain until this page is closed.',
  '请求超时，请稍后重试。': 'The request timed out. Please try again later.',
  '操作未完成，请稍后重试。': 'The operation could not be completed. Please try again later.',
  '当前测试会话不存在，请从管理员测试入口重新创建。':
    'This test session no longer exists. Create it again from the administrator test entry.',
  '该会话使用已废弃的当前表现格式，请返回沟通意图页重新生成。':
    'This session uses an obsolete current-performance format. Return to Conversation Intent and regenerate it.',
  '新会话创建失败，请稍后重试。': 'The new session could not be created. Please try again later.',
  '当前会话已失效，已创建新会话，请重新执行刚才的操作。':
    'The previous session expired. A new session has been created; please repeat the last action.',
  '请从管理员测试入口创建新的测试会话。':
    'Create a new test session from the administrator test entry.',
  '创建绩效反馈会话': 'Creating performance-feedback session',
  '页面数据载入失败，请刷新后重试。': 'Page data failed to load. Refresh and try again.',
  '已恢复该员工上次填写的背景信息、人格与诉求。':
    'The employee\'s previously entered background, persona, and motives were restored.',
  '读取该员工上次设置失败，已保留当前设置。':
    'The employee\'s previous settings could not be loaded. Current settings were preserved.',
  '已匹配员工信息': 'Employee matched',
  '请选择匹配结果': 'Select a matching result',
  '员工列表载入失败。': 'The employee list failed to load.',
  '匹配员工信息': 'Matching employee information',
  '未匹配到员工，请改用手动输入或上传资料。':
    'No employee was matched. Enter the information manually or upload a reference file.',
  '员工信息已选择，确认后将开启新的会话。':
    'Employee information selected. Confirming it will start a new session.',
  '员工信息已选择。': 'Employee information selected.',
  '请重新选择或忽略上次文件。': 'Select the previous file again or dismiss it.',
  '请先匹配员工、输入额外提供的信息或上传参考资料。':
    'Match an employee, enter additional information, or upload a reference file first.',
  '请先从员工库匹配并选择员工。': 'Match and select an employee from the directory first.',
  '额外提供的信息已保存到当前会话。': 'The additional information was saved to this session.',
  '员工信息已确认': 'Employee information confirmed',
  '确认员工信息': 'Confirming employee information',
  '意图已确认': 'Conversation intent confirmed',
  '确认意图': 'Confirming conversation intent',
  '请选择对话意图。': 'Select a conversation intent.',
  '请填写并确认员工当前表现。': 'Complete and confirm the employee\'s current performance.',
  '人格与诉求已确认': 'Persona and motives confirmed',
  '确认人格与诉求': 'Confirming persona and motives',
  '请选择主诉求。': 'Select a primary motive.',
  '辅诉求最多选择两个。': 'Select no more than two secondary motives.',
  '主诉求和辅诉求不能重复。': 'The primary and secondary motives cannot be the same.',
  '辅诉求不能重复。': 'Secondary motives cannot be duplicated.',
  '请先完成员工信息、意图、人格与诉求设置。':
    'Complete the employee, intent, persona, and motive settings first.',
  '谈前指导载入失败，请稍后重试。': 'Preparation guidance failed to load. Please try again later.',
  '该部分生成失败。': 'This section failed to generate.',
  '谈前指导部分生成失败，请重新生成。':
    'Part of the preparation guidance failed to generate. Please regenerate it.',
  '流式生成中断，已使用普通模式完成谈前指导。':
    'Streaming was interrupted. Preparation guidance was completed in standard mode.',
  '谈前指导生成中断，请重新生成。': 'Preparation guidance was interrupted. Please regenerate it.',
  '谈前指导流结束，但缺少最终结构化报告。':
    'The preparation-guidance stream ended without a final structured report.',
  '谈前指导生成失败，请重新生成。': 'Preparation guidance failed to generate. Please regenerate it.',
  '谈前指导 Word 已导出': 'Preparation guidance Word document exported',
  '谈前指导导出失败。': 'The preparation guidance could not be exported.',
  '员工正在回复中，请等本轮回复结束后再调整模拟设定。':
    'The employee is responding. Wait for this turn to finish before changing simulation settings.',
  '更新模拟设定': 'Updating simulation settings',
  '会话背景已清空': 'Session context cleared',
  '会话背景已更新': 'Session context updated',
  '上一轮员工回复尚未完成，本条消息已保留在输入队列中。':
    'The previous employee response is still running. This message remains in the input queue.',
  '会话已变化，未发送的追加消息已暂停，请确认后重试。':
    'The session changed, so unsent follow-up messages were paused. Confirm the session and try again.',
  '请先输入新增信息或模拟要求。': 'Enter new information or rehearsal instructions first.',
  '当前没有需要重试的追加消息。': 'There are no follow-up messages waiting to be retried.',
  '流式预演中断，已使用普通模式完成本轮回复。':
    'Rehearsal streaming was interrupted. This response was completed in standard mode.',
  '流式连接中断，本轮已切换为文字回复。':
    'Streaming was interrupted. This turn switched to a text response.',
  '本轮员工回复生成失败，请重试。': 'The employee response failed to generate. Please try again.',
  '员工搜索失败，请稍后重试。': 'Employee search failed. Please try again later.',
  '结束预演': 'Ending rehearsal',
  '复盘报告部分生成失败，请重试。': 'Part of the review report failed to generate. Please try again.',
  '复盘报告载入失败，请稍后重试。': 'The review report failed to load. Please try again later.',
  '请先完成至少一轮多轮预演，再生成复盘报告。':
    'Complete at least one rehearsal turn before generating the review report.',
  '流式复盘中断，普通模式仍有部分维度失败，请重试。':
    'Review streaming was interrupted, and some dimensions also failed in standard mode. Please try again.',
  '流式复盘中断，已使用普通模式完成报告。':
    'Review streaming was interrupted. The report was completed in standard mode.',
  '复盘报告生成中断，请重新生成。': 'Review report generation was interrupted. Please regenerate it.',
  '复盘报告生成失败，请重新生成。': 'The review report failed to generate. Please regenerate it.',
  '语言设置同步失败，请稍后重试。': 'The language setting could not be synchronized. Please try again later.',
  '语言设置同步失败，已恢复为会话原语言，请重试。':
    'The language setting could not be synchronized. The interface was restored to the session language; please try again.',
  '语言已切换，已忽略切换前生成的结果，请重试当前操作。':
    'The language changed, so the previous-language result was ignored. Please retry this operation.',
  '语言已切换，请先返回沟通意图页确认新语言的员工表现。':
    'The language changed. Return to Conversation Intent and confirm the employee-performance content in the new language first.',
  '语言设置尚未同步，暂时无法开始语音输入。':
    'The language setting is not synchronized, so speech input cannot start yet.',
  '语言已切换，本次录音已取消，请使用新语言重新录音。':
    'The language changed, so this recording was cancelled. Record again in the new language.',
  '语言已切换，员工表现初稿已按新语言重新生成，请返回沟通意图页确认。':
    'The language has changed and the employee-performance draft was regenerated. Return to Conversation Intent to review and confirm it.',
  '语言已切换，但员工表现初稿未能重新生成，请返回沟通意图页重试。':
    'The language changed, but the employee-performance draft could not be regenerated. Return to Conversation Intent and try again.',
  '实时语音准备中。': 'Preparing real-time speech input.',
  '实时语音状态暂不可用，仍可录音并在停止后完成转写。':
    'Real-time speech status is temporarily unavailable. You can still record and transcribe after stopping.',
  '当前业务会话尚未就绪，无法开始语音输入。':
    'The current session is not ready for speech input.',
  '语音录音服务连接超时，请稍后重试。':
    'The speech-recording service connection timed out. Please try again later.',
  '语音录音启动已取消。': 'Speech recording was cancelled.',
  '实时语音预览暂不可用，完整录音仍在继续。':
    'Real-time speech preview is temporarily unavailable. Full recording is continuing.',
  '语音录音或转写失败，请重试。': 'Speech recording or transcription failed. Please try again.',
  '语音录音连接失败，请重试。': 'The speech-recording connection failed. Please try again.',
  '语音录音服务未能建立连接。': 'The speech-recording service could not connect.',
  '语音录音连接已中断，未生成不完整的最终文本。':
    'The speech-recording connection was interrupted; no incomplete final text was produced.',
  '完整语音转写没有返回可用文本，请重试。':
    'The full speech transcription returned no usable text. Please try again.',
};

interface LanguageContextValue {
  language: AppLanguage;
  setLanguage: (language: AppLanguage) => void;
  toggleLanguage: () => void;
  translate: (source: string, english?: string) => string;
  translateTemplate: (
    source: string,
    english: string,
    values: TranslationTemplateValues,
  ) => string;
}

const LanguageContext = createContext<LanguageContextValue | null>(null);

export function getInitialLanguage(): AppLanguage {
  if (typeof window === 'undefined') return 'zh-CN';
  try {
    const stored = normalizeAppLanguage(window.localStorage.getItem(LANGUAGE_STORAGE_KEY));
    if (stored) return stored;
  } catch {
    // Browser preference detection below still works when storage is unavailable.
  }
  const browserLanguages = Array.isArray(window.navigator.languages)
    ? window.navigator.languages
    : [window.navigator.language];
  for (const browserLanguage of browserLanguages) {
    const supported = normalizeAppLanguage(browserLanguage);
    if (supported) return supported;
  }
  return 'zh-CN';
}

export function LanguageProvider({ children, initialLanguage }: {
  children: ReactNode;
  initialLanguage?: AppLanguage;
}) {
  const [{ language, catalog }, setLanguageState] = useState(() => {
    const selectedLanguage = initialLanguage ?? getInitialLanguage();
    return { language: selectedLanguage, catalog: languageCatalogs.get(selectedLanguage) };
  });
  const [languageChanges] = useState(() => createLanguageChangeHandler(
    language,
    languageCatalogs,
    (nextLanguage) => setLanguageState((current) => {
      const nextCatalog = languageCatalogs.get(nextLanguage);
      return current.language === nextLanguage && current.catalog === nextCatalog
        ? current
        : { language: nextLanguage, catalog: nextCatalog };
    }),
    (error) => console.warn('Unable to load the selected language.', error),
  ));
  const { setLanguage } = languageChanges;
  setCurrentLanguage(language);

  useEffect(() => () => languageChanges.cancel(), [languageChanges]);

  useEffect(() => {
    document.documentElement.lang = language;
    document.documentElement.dataset.language = language;
    try {
      window.localStorage.setItem(LANGUAGE_STORAGE_KEY, language);
    } catch {
      // Language switching must still work when browser storage is unavailable.
    }
  }, [language]);

  useEffect(() => {
    const syncLanguage = (event: StorageEvent) => {
      if (event.key !== LANGUAGE_STORAGE_KEY) return;
      const synchronizedLanguage = normalizeAppLanguage(event.newValue);
      if (synchronizedLanguage) setLanguage(synchronizedLanguage);
    };
    window.addEventListener('storage', syncLanguage);
    return () => window.removeEventListener('storage', syncLanguage);
  }, [setLanguage]);

  const toggleLanguage = useCallback(() => {
    const index = SUPPORTED_LANGUAGES.indexOf(languageChanges.requestedLanguage);
    setLanguage(SUPPORTED_LANGUAGES[(index + 1) % SUPPORTED_LANGUAGES.length]);
  }, [languageChanges, setLanguage]);

  const translate = useCallback((source: string, english?: string) => {
    if (language === 'zh-CN') return source;
    const englishFallback = english ?? ENGLISH_TEXT[source] ?? source;
    return catalog?.[source] ?? englishFallback;
  }, [catalog, language]);

  const translateTemplate = useCallback((
    source: string,
    english: string,
    values: TranslationTemplateValues,
  ) => formatTranslationTemplate(translate(source, english), values), [translate]);

  const value = useMemo<LanguageContextValue>(() => ({
    language,
    setLanguage,
    toggleLanguage,
    translate,
    translateTemplate,
  }), [language, setLanguage, toggleLanguage, translate, translateTemplate]);

  return <LanguageContext.Provider value={value}>{children}</LanguageContext.Provider>;
}

export function useLanguage(): LanguageContextValue {
  const value = useContext(LanguageContext);
  if (!value) throw new Error('useLanguage must be used inside LanguageProvider.');
  return value;
}

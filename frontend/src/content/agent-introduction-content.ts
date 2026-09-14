import type { StepKey } from '../types/domain';
import type { AppLanguage } from '../i18n/languageRuntime';

export interface LocalizedIntroductionText {
  readonly zh: string;
  readonly en: string;
  readonly de: string;
  readonly ja: string;
}

export interface AgentIntroductionGuide {
  readonly task: LocalizedIntroductionText;
  readonly meaning: LocalizedIntroductionText;
}

type LocalizedPair = readonly [LocalizedIntroductionText, LocalizedIntroductionText];

export interface AgentJourneyPreviewContentMap {
  readonly profile: {
    readonly heading: LocalizedIntroductionText;
    readonly metadata: readonly [
      LocalizedIntroductionText,
      LocalizedIntroductionText,
      LocalizedIntroductionText,
      LocalizedIntroductionText,
      LocalizedIntroductionText,
    ];
    readonly goalsHeading: LocalizedIntroductionText;
    readonly goals: LocalizedPair;
  };
  readonly intent: {
    readonly heading: LocalizedIntroductionText;
    readonly options: readonly [
      LocalizedIntroductionText,
      LocalizedIntroductionText,
      LocalizedIntroductionText,
      LocalizedIntroductionText,
      LocalizedIntroductionText,
    ];
  };
  readonly intentPerformance: {
    readonly heading: LocalizedIntroductionText;
    readonly goalHeading: LocalizedIntroductionText;
    readonly goal: LocalizedIntroductionText;
    readonly performanceHeading: LocalizedIntroductionText;
    readonly performance: LocalizedIntroductionText;
  };
  readonly simulation: {
    readonly heading: LocalizedIntroductionText;
    readonly traits: readonly [
      LocalizedIntroductionText,
      LocalizedIntroductionText,
      LocalizedIntroductionText,
    ];
    readonly primaryNeed: LocalizedIntroductionText;
    readonly supportingNeeds: LocalizedPair;
  };
  readonly guidance: {
    readonly heading: LocalizedIntroductionText;
    readonly dimensions: readonly [
      LocalizedIntroductionText,
      LocalizedIntroductionText,
      LocalizedIntroductionText,
      LocalizedIntroductionText,
    ];
    readonly detail: LocalizedIntroductionText;
  };
  readonly rehearsal: {
    readonly heading: LocalizedIntroductionText;
    readonly manager: LocalizedIntroductionText;
    readonly employee: LocalizedIntroductionText;
    readonly voice: LocalizedIntroductionText;
    readonly emotion: LocalizedIntroductionText;
  };
  readonly report: {
    readonly heading: LocalizedIntroductionText;
    readonly dimensions: readonly [
      LocalizedIntroductionText,
      LocalizedIntroductionText,
      LocalizedIntroductionText,
      LocalizedIntroductionText,
    ];
    readonly improvement: LocalizedIntroductionText;
    readonly trajectory: LocalizedIntroductionText;
  };
}

export interface AgentIntroductionJourneyContent {
  readonly startTitle: LocalizedIntroductionText;
  readonly startDescription: LocalizedIntroductionText;
  readonly taskLabel: LocalizedIntroductionText;
  readonly meaningLabel: LocalizedIntroductionText;
  readonly performanceTitle: LocalizedIntroductionText;
  readonly endTitle: LocalizedIntroductionText;
  readonly endDescription: LocalizedIntroductionText;
  readonly previews: AgentJourneyPreviewContentMap;
}

export interface AgentIntroductionStep {
  readonly id: StepKey;
  readonly title: LocalizedIntroductionText;
  readonly guide: AgentIntroductionGuide;
  readonly performanceGuide?: AgentIntroductionGuide;
  readonly action: readonly LocalizedIntroductionText[];
  readonly options?: readonly LocalizedIntroductionText[];
  readonly note?: LocalizedIntroductionText;
  readonly meaning: LocalizedIntroductionText;
}

export interface AgentIntroductionContent {
  readonly title: string;
  readonly subtitle: LocalizedIntroductionText;
  readonly purposeTitle: LocalizedIntroductionText;
  readonly purpose: readonly LocalizedIntroductionText[];
  readonly stepsTitle: LocalizedIntroductionText;
  readonly journey: AgentIntroductionJourneyContent;
  readonly startLabel: LocalizedIntroductionText;
  readonly startingLabel: LocalizedIntroductionText;
  readonly backLabel: LocalizedIntroductionText;
  readonly steps: readonly AgentIntroductionStep[];
}

export type AgentJourneyStepId = StepKey | 'intent-performance';
export type AgentJourneyPreviewId = keyof AgentJourneyPreviewContentMap;

export interface AgentJourneyDisplayStep {
  readonly id: AgentJourneyStepId;
  readonly number: string;
  readonly title: LocalizedIntroductionText;
  readonly guide: AgentIntroductionGuide;
  readonly previewId: AgentJourneyPreviewId;
}

const INTRODUCTION_GERMAN_TEXT: Readonly<Record<string, string>> = {
  '为重要的绩效反馈做好准备，并在真实沟通前完成一次安全、完整的演练。':
    'Bereiten Sie sich auf wichtiges Performance-Feedback vor und erproben Sie das Gespräch vorab einmal vollständig in einem sicheren Rahmen.',
  '设计目的': 'Zweck',
  'Performance Feedback 不替代管理者作出绩效判断，也不替代 HR 或 Legal 的专业意见。':
    'Performance Feedback ersetzt weder die Leistungsbeurteilung durch die Führungskraft noch die fachliche Beratung durch HR oder Legal.',
  '它通过“理解背景、明确意图、理解员工、谈前准备、对话预演、复盘改进”六个步骤，帮助管理者把已有判断转化为更清晰的表达、更有效的沟通和可执行的后续行动。':
    'In sechs Schritten – Kontext verstehen, Absicht klären, Mitarbeitende verstehen, Gespräch vorbereiten, Dialog erproben und auswerten – hilft es Führungskräften, ihre Einschätzung in klarere Aussagen, wirksamere Gespräche und umsetzbare Folgemaßnahmen zu übersetzen.',
  '六个步骤': 'Sechs Schritte',
  '准备一次重要的绩效反馈': 'Ein wichtiges Performance-Gespräch vorbereiten',
  '从真实背景出发，沿着六个步骤把判断转化为清晰表达、有效互动和可执行行动。':
    'Beginnen Sie mit dem tatsächlichen Kontext und führen Sie Ihre Einschätzung in sechs Schritten zu klaren Aussagen, wirksamer Interaktion und umsetzbaren Maßnahmen.',
  '需要完成': 'Ihre Aufgabe',
  '为什么重要': 'Warum das wichtig ist',
  '员工表现': 'Leistung des Mitarbeitenden',
  '把判断转化为一次更清晰的沟通': 'Aus einer Einschätzung ein klareres Gespräch machen',
  '准备完成后进入工作台，员工资料、沟通意图和预演过程会在同一会话中连续保存。':
    'Nach der Vorbereitung wechseln Sie in den Arbeitsbereich. Mitarbeiterdaten, Gesprächsabsicht und Probedialog bleiben in derselben Sitzung miteinander verknüpft.',
  '员工档案': 'Mitarbeiterprofil',
  '示例员工': 'Beispielprofil',
  '产品与工程': 'Produkt & Entwicklung',
  '产品负责人': 'Produktleitung',
  '职级 G9': 'Stufe G9',
  '绩效 2': 'Leistungsbewertung 2',
  '关键目标': 'Kernziele',
  '提升核心产品采用率': 'Nutzung des Kernprodukts steigern',
  '建立跨团队交付节奏': 'Einen teamübergreifenden Lieferrhythmus etablieren',
  '沟通方向': 'Gesprächsausrichtung',
  '发展': 'Entwicklung',
  '改进': 'Verbesserung',
  '退出': 'Austritt',
  '发展与改进': 'Entwicklung und Verbesserung',
  '改进与退出预警': 'Verbesserung mit Austrittswarnung',
  '员工表现推演': 'Entwurf der Mitarbeiterleistung',
  '当前目标': 'Aktuelles Ziel',
  '提高重点项目的交付质量': 'Lieferqualität des Schwerpunktprojekts verbessern',
  '可能的当前表现': 'Mögliche aktuelle Leistung',
  '关键节点按期完成，跨团队推进仍需加强。':
    'Wichtige Meilensteine wurden termingerecht erreicht; die teamübergreifende Umsetzung muss noch verbessert werden.',
  '人格与诉求': 'Persönlichkeit und Anliegen',
  '开放性': 'Offenheit',
  '尽责性': 'Gewissenhaftigkeit',
  '宜人性': 'Verträglichkeit',
  '主诉求 · 认可与发展': 'Hauptanliegen · Anerkennung und Entwicklung',
  '清晰标准': 'Klare Standards',
  '资源支持': 'Ressourcenunterstützung',
  '谈前准备': 'Gesprächsvorbereitung',
  '开场定调': 'Gesprächseinstieg und Ausrichtung',
  '情绪承接': 'Emotionale Reaktion aufgreifen',
  '产出与标准': 'Ergebnisse und Standards',
  '发展计划': 'Entwicklungsplan',
  '先确认沟通目的，再用具体事实说明判断依据。':
    'Klären Sie zunächst den Gesprächszweck und erläutern Sie die Einschätzung dann anhand konkreter Fakten.',
  '对话预演': 'Gesprächssimulation',
  '我想先听听你对这一阶段结果的看法。':
    'Ich möchte zuerst hören, wie Sie die Ergebnisse dieser Phase einschätzen.',
  '我认可已经取得的进展，也想了解下一阶段的具体标准。':
    'Ich sehe die erzielten Fortschritte und möchte außerdem wissen, welche konkreten Standards für die nächste Phase gelten.',
  '语音输入': 'Spracheingabe',
  '平静 · 积极投入': 'Ruhig · Engagiert',
  '复盘结果': 'Auswertung',
  '开场定调 4/5': 'Gesprächseinstieg und Ausrichtung 4/5',
  '情绪承接 4/5': 'Emotionale Reaktion 4/5',
  '产出与标准 3/5': 'Ergebnisse und Standards 3/5',
  '发展计划 4/5': 'Entwicklungsplan 4/5',
  '补充衡量标准和明确时间节点。': 'Messbare Kriterien und einen klaren Zeitrahmen ergänzen.',
  '情绪轨迹': 'Emotionsverlauf',
  '开始准备': 'Vorbereitung starten',
  '正在创建会话': 'Sitzung wird erstellt',
  '返回首页': 'Zur Startseite',
  '员工信息': 'Mitarbeiterprofil',
  '选择本次沟通的员工，核对其部门、岗位、职级、绩效与工作目标，并补充系统尚未包含的背景信息。':
    'Wählen Sie den Mitarbeitenden für dieses Gespräch aus, prüfen Sie Abteilung, Rolle, Stufe, Leistung und Arbeitsziele und ergänzen Sie relevante Hintergrundinformationen, die noch nicht im System enthalten sind.',
  '让后续建议建立在真实员工资料和工作目标上，减少笼统判断，避免生成与员工实际岗位无关的内容。':
    'So stützen sich die folgenden Empfehlungen auf echte Mitarbeiterdaten und Arbeitsziele, vermeiden pauschale Urteile und bleiben für die tatsächliche Rolle relevant.',
  '沟通意图': 'Gesprächsabsicht',
  '选择本次沟通的主要方向：发展 \\ 改进 \\ 退出 \\ 发展与改进 \\ 改进与退出预警':
    'Wählen Sie die Hauptausrichtung: Entwicklung, Verbesserung, Austritt, Entwicklung und Verbesserung oder Verbesserung mit Austrittswarnung.',
  '同一项绩效结果，在不同沟通意图下需要不同的重点、边界和表达方式。明确意图可以避免对话偏离目的。':
    'Dasselbe Leistungsergebnis erfordert je nach Gesprächsabsicht andere Schwerpunkte, Grenzen und Formulierungen. Eine klare Absicht hält das Gespräch auf sein Ziel ausgerichtet.',
  '系统会结合员工目标、岗位、职级和绩效信息，推演员工当前可能的表现。你需要核对并编辑这些内容，确保它符合你掌握的事实。':
    'Das System erstellt anhand von Zielen, Rolle, Stufe und Leistungsinformationen einen Entwurf der möglichen aktuellen Leistung. Prüfen und bearbeiten Sie ihn, damit er den Ihnen bekannten Fakten entspricht.',
  '同一项绩效结果，在不同沟通意图下需要不同的重点、边界和表达方式。明确意图可以避免对话偏离目的，也不会让模型替你作出正式管理决定。':
    'Dasselbe Leistungsergebnis erfordert je nach Gesprächsabsicht andere Schwerpunkte, Grenzen und Formulierungen. Eine klare Absicht hält das Gespräch auf sein Ziel ausgerichtet, ohne dass das Modell formelle Führungsentscheidungen für Sie trifft.',
  '选择本次沟通的主要方向：': 'Wählen Sie die Hauptausrichtung dieses Gesprächs:',
  '设置员工的大五人格倾向，选择一项主诉求，并补充最多两项辅诉求。':
    'Legen Sie die Big-Five-Tendenzen des Mitarbeitenden fest, wählen Sie ein Hauptanliegen und ergänzen Sie bis zu zwei weitere Anliegen.',
  '人格决定员工可能采用怎样的措辞、语气和互动姿态；诉求决定员工在对话中真正关注什么。它们共同帮助预演呈现更贴近真实员工的反应，但不会改变员工资料和绩效事实。':
    'Die Persönlichkeit prägt Wortwahl, Ton und Interaktion; die Anliegen bestimmen, was dem Mitarbeitenden im Gespräch wirklich wichtig ist. Beides macht die Probe realistischer, ohne Mitarbeiterdaten oder Leistungsfakten zu verändern.',
  '谈前指导': 'Gesprächsvorbereitung',
  '查看并筛选四个维度的准备建议：- 开场定调 - 情绪承接 - 产出与标准 - 发展计划。点击要点可以查看具体建议和参考依据，也可以导出 Word 文档用于面谈准备。':
    'Prüfen und filtern Sie die Vorbereitungshinweise in vier Dimensionen: Gesprächseinstieg und Ausrichtung, emotionale Reaktion, Ergebnisse und Standards sowie Entwicklungsplanung. Öffnen Sie einen Punkt für konkrete Hinweise und Quellen oder exportieren Sie ein Word-Dokument zur Gesprächsvorbereitung.',
  '在正式表达前提前检查谈话结构、事实依据、员工可能的反应和后续安排，减少临场遗漏与表达失误。':
    'Prüfen Sie vor dem Gespräch Aufbau, Faktenbasis, mögliche Reaktionen und Folgemaßnahmen, um Auslassungen und vermeidbare Formulierungsfehler zu reduzieren.',
  '查看并筛选四个维度的准备建议：': 'Prüfen und filtern Sie Hinweise für vier Vorbereitungsdimensionen:',
  '点击要点可以查看具体建议和参考依据，也可以导出 Word 文档用于面谈准备。':
    'Öffnen Sie einen Punkt für konkrete Hinweise und Quellen oder exportieren Sie ein Word-Dokument zur Gesprächsvorbereitung.',
  '多轮预演': 'Probedialog',
  '通过文字或语音输入经理的话语，与模拟员工进行多轮沟通。根据员工的回复和情绪变化调整表达，必要时可以补充临时背景设定。':
    'Führen Sie per Text- oder Spracheingabe einen mehrteiligen Dialog mit dem simulierten Mitarbeitenden. Passen Sie Ihre Formulierungen an Antworten und emotionale Veränderungen an und ergänzen Sie bei Bedarf vorübergehenden Kontext.',
  '把“知道应该怎么说”转化为“能够在真实反应下说清楚”。你可以安全地尝试不同表达，观察员工可能产生的情绪、质疑和防御，并练习如何继续推进对话。':
    'Machen Sie aus dem Wissen, was Sie sagen sollten, die Fähigkeit, auch bei realistischen Reaktionen klar zu kommunizieren. Erproben Sie sicher unterschiedliche Formulierungen, beobachten Sie mögliche Emotionen, Rückfragen und Abwehr und üben Sie, das Gespräch weiterzuführen.',
  '右侧四个维度会提示当前对话已经实质涉及的部分，但它们不是最终评分。':
    'Die vier Dimensionen rechts zeigen, welche Bereiche im Gespräch bereits substanziell behandelt wurden; sie sind keine Endbewertung.',
  '复盘报告': 'Auswertungsbericht',
  '结束预演后，查看四个维度的评分、评分依据、有待改进之处、建议表达和员工情绪轨迹，并导出完整报告。':
    'Sehen Sie nach dem Probedialog Bewertungen, Begründungen, Verbesserungsfelder, Formulierungsvorschläge und den Emotionsverlauf in vier Dimensionen ein und exportieren Sie den vollständigen Bericht.',
  '帮助你识别哪些表达已经有效，哪些地方仍然模糊、缺少依据或未能承接员工反应，并把问题转化为下一次可以直接练习的具体话术。':
    'Erkennen Sie, welche Formulierungen funktionieren und welche noch unklar, unbelegt oder nicht auf die Reaktion des Mitarbeitenden abgestimmt sind, und übersetzen Sie Probleme in konkrete Aussagen für die nächste Übung.',
};

const INTRODUCTION_JAPANESE_TEXT: Readonly<Record<string, string>> = {
  '为重要的绩效反馈做好准备，并在真实沟通前完成一次安全、完整的演练。':
    '重要なパフォーマンスフィードバックに備え、本番の対話前に安全な環境で一連の流れを練習します。',
  '设计目的': '目的',
  'Performance Feedback 不替代管理者作出绩效判断，也不替代 HR 或 Legal 的专业意见。':
    'Performance Feedback は、マネージャーによる評価判断や、HR・Legal の専門的な助言に代わるものではありません。',
  '它通过“理解背景、明确意图、理解员工、谈前准备、对话预演、复盘改进”六个步骤，帮助管理者把已有判断转化为更清晰的表达、更有效的沟通和可执行的后续行动。':
    '背景理解、意図の明確化、従業員理解、事前準備、対話リハーサル、振り返りと改善の6ステップで、マネージャーの判断を明確な表現、効果的な対話、実行可能な次の行動へつなげます。',
  '六个步骤': '6つのステップ',
  '准备一次重要的绩效反馈': '重要なパフォーマンス対話を準備する',
  '从真实背景出发，沿着六个步骤把判断转化为清晰表达、有效互动和可执行行动。':
    '実際の背景から始め、6つのステップで判断を明確な表現、効果的なやり取り、実行可能な行動へつなげます。',
  '需要完成': '行うこと',
  '为什么重要': '重要な理由',
  '员工表现': '従業員のパフォーマンス',
  '把判断转化为一次更清晰的沟通': '判断をより明確な対話に変える',
  '准备完成后进入工作台，员工资料、沟通意图和预演过程会在同一会话中连续保存。':
    '準備後はワークスペースに進みます。従業員情報、対話の意図、リハーサルの進捗は同じセッション内で連携して保存されます。',
  '员工档案': '従業員プロフィール',
  '示例员工': '従業員の例',
  '产品与工程': '製品・エンジニアリング',
  '产品负责人': 'プロダクト責任者',
  '职级 G9': '職級 G9',
  '绩效 2': 'パフォーマンス評価 2',
  '关键目标': '主要目標',
  '提升核心产品采用率': '主力製品の導入率を高める',
  '建立跨团队交付节奏': 'チーム横断のデリバリーリズムを確立する',
  '沟通方向': '対話の方向性',
  '发展': '育成',
  '改进': '改善',
  '退出': '退職',
  '发展与改进': '育成と改善',
  '改进与退出预警': '改善と退職予告',
  '员工表现推演': '従業員のパフォーマンス案',
  '当前目标': '現在の目標',
  '提高重点项目的交付质量': '重点プロジェクトのデリバリー品質を高める',
  '可能的当前表现': '現在想定されるパフォーマンス',
  '关键节点按期完成，跨团队推进仍需加强。':
    '主要なマイルストーンは期日どおりに完了していますが、チーム横断の推進にはなお改善が必要です。',
  '人格与诉求': '性格と要望',
  '开放性': '開放性',
  '尽责性': '誠実性',
  '宜人性': '協調性',
  '主诉求 · 认可与发展': '主な要望 · 承認と成長',
  '清晰标准': '明確な基準',
  '资源支持': 'リソース支援',
  '谈前准备': '対話の事前準備',
  '开场定调': '導入と認識合わせ',
  '情绪承接': '感情の受け止め',
  '产出与标准': '成果と基準',
  '发展计划': '育成計画',
  '先确认沟通目的，再用具体事实说明判断依据。':
    'まず対話の目的を確認し、その後、具体的な事実に基づいて判断根拠を説明します。',
  '对话预演': '対話リハーサル',
  '我想先听听你对这一阶段结果的看法。':
    'まず、この期間の結果をどう捉えているか聞かせてください。',
  '我认可已经取得的进展，也想了解下一阶段的具体标准。':
    'これまでの進捗は認識しています。次の段階の具体的な基準も知りたいです。',
  '语音输入': '音声入力',
  '平静 · 积极投入': '平静 · 前向き',
  '复盘结果': '振り返り結果',
  '开场定调 4/5': '導入と認識合わせ 4/5',
  '情绪承接 4/5': '感情の受け止め 4/5',
  '产出与标准 3/5': '成果と基準 3/5',
  '发展计划 4/5': '育成計画 4/5',
  '补充衡量标准和明确时间节点。': '測定可能な基準と明確な期限を追加します。',
  '情绪轨迹': '感情の推移',
  '开始准备': '準備を始める',
  '正在创建会话': 'セッションを作成しています',
  '返回首页': 'ホームへ戻る',
  '员工信息': '従業員情報',
  '选择本次沟通的员工，核对其部门、岗位、职级、绩效与工作目标，并补充系统尚未包含的背景信息。':
    '今回の対話対象となる従業員を選び、部門、役割、職級、パフォーマンス、業務目標を確認し、システムにまだ含まれていない関連背景を追加します。',
  '让后续建议建立在真实员工资料和工作目标上，减少笼统判断，避免生成与员工实际岗位无关的内容。':
    '以降の提案を実際の従業員情報と業務目標に基づかせ、抽象的な判断や、実際の役割と無関係な内容を避けられます。',
  '沟通意图': '対話の意図',
  '选择本次沟通的主要方向：发展 \\ 改进 \\ 退出 \\ 发展与改进 \\ 改进与退出预警':
    '今回の対話の主な方向性を選びます：育成、改善、退職、育成と改善、改善と退職予告。',
  '同一项绩效结果，在不同沟通意图下需要不同的重点、边界和表达方式。明确意图可以避免对话偏离目的。':
    '同じパフォーマンス結果でも、対話の意図によって重点、境界、表現方法は異なります。意図を明確にすることで、対話が目的から逸れるのを防ぎます。',
  '系统会结合员工目标、岗位、职级和绩效信息，推演员工当前可能的表现。你需要核对并编辑这些内容，确保它符合你掌握的事实。':
    'システムは従業員の目標、役割、職級、パフォーマンス情報から、現在想定されるパフォーマンスを作成します。把握している事実と一致するように確認・編集してください。',
  '同一项绩效结果，在不同沟通意图下需要不同的重点、边界和表达方式。明确意图可以避免对话偏离目的，也不会让模型替你作出正式管理决定。':
    '同じパフォーマンス結果でも、対話の意図によって重点、境界、表現方法は異なります。意図を明確にすることで目的からの逸脱を防ぎ、モデルが代わりに正式なマネジメント判断を行うこともありません。',
  '选择本次沟通的主要方向：': '今回の対話の主な方向性を選びます：',
  '设置员工的大五人格倾向，选择一项主诉求，并补充最多两项辅诉求。':
    '従業員のビッグファイブの傾向を設定し、主な要望を1つ、補助的な要望を最大2つ選びます。',
  '人格决定员工可能采用怎样的措辞、语气和互动姿态；诉求决定员工在对话中真正关注什么。它们共同帮助预演呈现更贴近真实员工的反应，但不会改变员工资料和绩效事实。':
    '性格は従業員の言葉選び、語調、対話姿勢に影響し、要望は対話で本当に重視することを定めます。両方を組み合わせることで、従業員情報やパフォーマンス事実を変えずに、より現実的な反応を再現します。',
  '谈前指导': '事前ガイダンス',
  '查看并筛选四个维度的准备建议：- 开场定调 - 情绪承接 - 产出与标准 - 发展计划。点击要点可以查看具体建议和参考依据，也可以导出 Word 文档用于面谈准备。':
    '4つの観点—導入と認識合わせ、感情の受け止め、成果と基準、育成計画—の準備ガイダンスを確認・選択します。項目を開いて具体的な提案と参考根拠を確認したり、面談準備用の Word 文書をエクスポートしたりできます。',
  '在正式表达前提前检查谈话结构、事实依据、员工可能的反应和后续安排，减少临场遗漏与表达失误。':
    '本番の対話前に、構成、事実根拠、従業員の反応、その後の対応を確認し、言い漏らしや表現上のミスを減らします。',
  '查看并筛选四个维度的准备建议：': '4つの観点の準備ガイダンスを確認・選択します：',
  '点击要点可以查看具体建议和参考依据，也可以导出 Word 文档用于面谈准备。':
    '項目を開いて具体的な提案と参考根拠を確認したり、面談準備用の Word 文書をエクスポートしたりできます。',
  '多轮预演': '複数ターンのリハーサル',
  '通过文字或语音输入经理的话语，与模拟员工进行多轮沟通。根据员工的回复和情绪变化调整表达，必要时可以补充临时背景设定。':
    'テキストまたは音声でマネージャーの発言を入力し、シミュレートされた従業員と複数ターンの対話を行います。従業員の返答や感情の変化に合わせて表現を調整し、必要に応じて一時的な背景情報を追加します。',
  '把“知道应该怎么说”转化为“能够在真实反应下说清楚”。你可以安全地尝试不同表达，观察员工可能产生的情绪、质疑和防御，并练习如何继续推进对话。':
    '「何を言うべきか知っている」を「現実的な反応に対しても明確に伝えられる」に変えます。安全に異なる表現を試し、従業員に生じ得る感情、疑問、防御的な反応を観察しながら、対話を前に進める練習をします。',
  '右侧四个维度会提示当前对话已经实质涉及的部分，但它们不是最终评分。':
    '右側の4つの観点は、現在の対話で実質的に扱われた領域を示すもので、最終評価ではありません。',
  '复盘报告': '振り返りレポート',
  '结束预演后，查看四个维度的评分、评分依据、有待改进之处、建议表达和员工情绪轨迹，并导出完整报告。':
    'リハーサル後、4つの観点のスコア、評価根拠、改善点、推奨される表現、従業員の感情の推移を確認し、レポート全体をエクスポートします。',
  '帮助你识别哪些表达已经有效，哪些地方仍然模糊、缺少依据或未能承接员工反应，并把问题转化为下一次可以直接练习的具体话术。':
    '効果的だった表現と、まだ不明確、根拠不足、または従業員の反応を受け止められていない箇所を特定し、次回直接練習できる具体的な言い方に変えます。',
};

const text = (zh: string, en: string): LocalizedIntroductionText => {
  const de = INTRODUCTION_GERMAN_TEXT[zh];
  const ja = INTRODUCTION_JAPANESE_TEXT[zh];
  if (!de || !ja) throw new Error(`Missing Agent introduction translation: ${zh}`);
  return { zh, en, de, ja };
};

export function localizeIntroductionText(
  value: LocalizedIntroductionText,
  language: AppLanguage,
): string {
  if (language === 'zh-CN') return value.zh;
  return value[language];
}
const guide = (
  taskZh: string,
  taskEn: string,
  meaningZh: string,
  meaningEn: string,
): AgentIntroductionGuide => ({
  task: text(taskZh, taskEn),
  meaning: text(meaningZh, meaningEn),
});

export const agentIntroductionContent = {
  title: 'Performance Feedback',
  subtitle: text(
    '为重要的绩效反馈做好准备，并在真实沟通前完成一次安全、完整的演练。',
    'Prepare for important performance feedback through one safe, complete rehearsal before the real conversation.',
  ),
  purposeTitle: text('设计目的', 'Purpose'),
  purpose: [
    text(
      'Performance Feedback 不替代管理者作出绩效判断，也不替代 HR 或 Legal 的专业意见。',
      'Performance Feedback does not replace a manager\'s performance judgment or professional advice from HR or Legal.',
    ),
    text(
      '它通过“理解背景、明确意图、理解员工、谈前准备、对话预演、复盘改进”六个步骤，帮助管理者把已有判断转化为更清晰的表达、更有效的沟通和可执行的后续行动。',
      'Through six steps - understand the context, clarify intent, understand the employee, prepare, rehearse, and review - it helps managers turn existing judgment into clearer language, more effective conversations, and actionable follow-up.',
    ),
  ],
  stepsTitle: text('六个步骤', 'Six steps'),
  journey: {
    startTitle: text(
      '准备一次重要的绩效反馈',
      'Prepare for an important performance conversation',
    ),
    startDescription: text(
      '从真实背景出发，沿着六个步骤把判断转化为清晰表达、有效互动和可执行行动。',
      'Start with real context and move through six steps to turn judgment into clear language, effective interaction, and actionable follow-up.',
    ),
    taskLabel: text('需要完成', 'What you do'),
    meaningLabel: text('为什么重要', 'Why it matters'),
    performanceTitle: text('员工表现', 'Employee performance'),
    endTitle: text(
      '把判断转化为一次更清晰的沟通',
      'Turn your judgment into a clearer conversation',
    ),
    endDescription: text(
      '准备完成后进入工作台，员工资料、沟通意图和预演过程会在同一会话中连续保存。',
      'Continue to the workspace when ready. Employee context, conversation intent, and rehearsal progress stay connected in one session.',
    ),
    previews: {
      profile: {
        heading: text('员工档案', 'Employee profile'),
        metadata: [
          text('示例员工', 'Sample employee'),
          text('产品与工程', 'Product & Engineering'),
          text('产品负责人', 'Product lead'),
          text('职级 G9', 'Level G9'),
          text('绩效 2', 'Performance 2'),
        ],
        goalsHeading: text('关键目标', 'Key goals'),
        goals: [
          text('提升核心产品采用率', 'Increase adoption of the core product'),
          text('建立跨团队交付节奏', 'Build a cross-team delivery rhythm'),
        ],
      },
      intent: {
        heading: text('沟通方向', 'Conversation direction'),
        options: [
          text('发展', 'Development'),
          text('改进', 'Improvement'),
          text('退出', 'Exit'),
          text('发展与改进', 'Development & improvement'),
          text('改进与退出预警', 'Improvement & exit warning'),
        ],
      },
      intentPerformance: {
        heading: text('员工表现推演', 'Employee performance draft'),
        goalHeading: text('当前目标', 'Current goal'),
        goal: text('提高重点项目的交付质量', 'Improve delivery quality for the priority initiative'),
        performanceHeading: text('可能的当前表现', 'Possible current performance'),
        performance: text('关键节点按期完成，跨团队推进仍需加强。', 'Key milestones are on time; cross-team execution still needs improvement.'),
      },
      simulation: {
        heading: text('人格与诉求', 'Personality and needs'),
        traits: [
          text('开放性', 'Openness'),
          text('尽责性', 'Conscientiousness'),
          text('宜人性', 'Agreeableness'),
        ],
        primaryNeed: text('主诉求 · 认可与发展', 'Primary need · Recognition and growth'),
        supportingNeeds: [
          text('清晰标准', 'Clear standards'),
          text('资源支持', 'Resource support'),
        ],
      },
      guidance: {
        heading: text('谈前准备', 'Conversation preparation'),
        dimensions: [
          text('开场定调', 'Opening and tone'),
          text('情绪承接', 'Emotional response'),
          text('产出与标准', 'Outcomes and standards'),
          text('发展计划', 'Development plan'),
        ],
        detail: text(
          '先确认沟通目的，再用具体事实说明判断依据。',
          'Confirm the purpose first, then explain the judgment with specific evidence.',
        ),
      },
      rehearsal: {
        heading: text('对话预演', 'Conversation rehearsal'),
        manager: text(
          '我想先听听你对这一阶段结果的看法。',
          'I would like to hear your view of the results from this period first.',
        ),
        employee: text(
          '我认可已经取得的进展，也想了解下一阶段的具体标准。',
          'I recognize the progress and would also like clarity on the standards for the next stage.',
        ),
        voice: text('语音输入', 'Voice input'),
        emotion: text('平静 · 积极投入', 'Calm · Engaged'),
      },
      report: {
        heading: text('复盘结果', 'Review results'),
        dimensions: [
          text('开场定调 4/5', 'Opening and tone 4/5'),
          text('情绪承接 4/5', 'Emotional response 4/5'),
          text('产出与标准 3/5', 'Outcomes and standards 3/5'),
          text('发展计划 4/5', 'Development plan 4/5'),
        ],
        improvement: text(
          '补充衡量标准和明确时间节点。',
          'Add measurable standards and a clear timeline.',
        ),
        trajectory: text('情绪轨迹', 'Emotion trajectory'),
      },
    },
  },
  startLabel: text('开始准备', 'Start preparing'),
  startingLabel: text('正在创建会话', 'Creating session'),
  backLabel: text('返回首页', 'Back to home'),
  steps: [
    {
      id: 'profile',
      title: text('员工信息', 'Employee information'),
      guide: guide(
        '选择本次沟通的员工，核对其部门、岗位、职级、绩效与工作目标，并补充系统尚未包含的背景信息。',
        'Select the employee, verify their department, role, level, performance, and goals, and add relevant context not yet in the system.',
        '让后续建议建立在真实员工资料和工作目标上，减少笼统判断，避免生成与员工实际岗位无关的内容。',
        'This grounds later guidance in real employee information and goals, reducing generic judgments and irrelevant content.',
      ),
      action: [text(
        '选择本次沟通的员工，核对其部门、岗位、职级、绩效与工作目标，并补充系统尚未包含的背景信息。',
        'Select the employee, verify their department, role, level, performance, and goals, and add relevant context not yet in the system.',
      )],
      meaning: text(
        '让后续建议建立在真实员工资料和工作目标上，减少笼统判断，避免生成与员工实际岗位无关的内容。',
        'This grounds later guidance in real employee information and goals, reducing generic judgments and irrelevant content.',
      ),
    },
    {
      id: 'intent',
      title: text('沟通意图', 'Conversation intent'),
      guide: guide(
        '选择本次沟通的主要方向：发展 \\ 改进 \\ 退出 \\ 发展与改进 \\ 改进与退出预警',
        'Choose the primary direction: development, improvement, exit, development and improvement, or improvement with an exit warning.',
        '同一项绩效结果，在不同沟通意图下需要不同的重点、边界和表达方式。明确意图可以避免对话偏离目的。',
        'The same performance result requires different emphasis, boundaries, and language for different intents. A clear intent keeps the conversation focused.',
      ),
      performanceGuide: guide(
        '系统会结合员工目标、岗位、职级和绩效信息，推演员工当前可能的表现。你需要核对并编辑这些内容，确保它符合你掌握的事实。',
        'The system uses the employee\'s goals, role, level, and performance information to draft a possible view of current performance. Review and edit it so it matches the facts you know.',
        '同一项绩效结果，在不同沟通意图下需要不同的重点、边界和表达方式。明确意图可以避免对话偏离目的，也不会让模型替你作出正式管理决定。',
        'The same performance result requires different emphasis, boundaries, and language for different intents. A clear intent keeps the conversation focused without allowing the model to make a formal management decision for you.',
      ),
      action: [
        text('选择本次沟通的主要方向：', 'Choose the primary direction for this conversation:'),
        text(
          '系统会结合员工目标、岗位、职级和绩效信息，推演员工当前可能的表现。你需要核对并编辑这些内容，确保它符合你掌握的事实。',
          'The system uses the employee\'s goals, role, level, and performance information to draft a possible view of current performance. Review and edit it so it matches the facts you know.',
        ),
      ],
      options: [
        text('发展', 'Development'),
        text('改进', 'Improvement'),
        text('退出', 'Exit'),
        text('发展与改进', 'Development and improvement'),
        text('改进与退出预警', 'Improvement and exit warning'),
      ],
      meaning: text(
        '同一项绩效结果，在不同沟通意图下需要不同的重点、边界和表达方式。明确意图可以避免对话偏离目的，也不会让模型替你作出正式管理决定。',
        'The same performance result requires different emphasis, boundaries, and language for different intents. A clear intent keeps the conversation focused without allowing the model to make a formal management decision for you.',
      ),
    },
    {
      id: 'simulation',
      title: text('人格与诉求', 'Personality and needs'),
      guide: guide(
        '设置员工的大五人格倾向，选择一项主诉求，并补充最多两项辅诉求。',
        'Set the employee\'s Big Five tendencies, choose one primary need, and add up to two supporting needs.',
        '人格决定员工可能采用怎样的措辞、语气和互动姿态；诉求决定员工在对话中真正关注什么。它们共同帮助预演呈现更贴近真实员工的反应，但不会改变员工资料和绩效事实。',
        'Personality shapes wording, tone, and interaction style, while needs shape what the employee truly cares about. Together they make the rehearsal more realistic without changing employee data or performance facts.',
      ),
      action: [text(
        '设置员工的大五人格倾向，选择一项主诉求，并补充最多两项辅诉求。',
        'Set the employee\'s Big Five tendencies, choose one primary need, and add up to two supporting needs.',
      )],
      meaning: text(
        '人格决定员工可能采用怎样的措辞、语气和互动姿态；诉求决定员工在对话中真正关注什么。它们共同帮助预演呈现更贴近真实员工的反应，但不会改变员工资料和绩效事实。',
        'Personality shapes wording, tone, and interaction style, while needs shape what the employee truly cares about. Together they make the rehearsal more realistic without changing employee data or performance facts.',
      ),
    },
    {
      id: 'guidance',
      title: text('谈前指导', 'Pre-conversation guidance'),
      guide: guide(
        '查看并筛选四个维度的准备建议：- 开场定调 - 情绪承接 - 产出与标准 - 发展计划。点击要点可以查看具体建议和参考依据，也可以导出 Word 文档用于面谈准备。',
        'Review preparation guidance across four dimensions: opening and tone, emotional response, outcomes and standards, and development planning. Open any key point for detailed guidance and references, or export a Word document for preparation.',
        '在正式表达前提前检查谈话结构、事实依据、员工可能的反应和后续安排，减少临场遗漏与表达失误。',
        'Check the conversation structure, evidence, possible employee reactions, and follow-up before speaking, reducing omissions and avoidable wording mistakes.',
      ),
      action: [text('查看并筛选四个维度的准备建议：', 'Review preparation guidance across four dimensions:')],
      options: [
        text('开场定调', 'Opening and tone'),
        text('情绪承接', 'Emotional response'),
        text('产出与标准', 'Outcomes and standards'),
        text('发展计划', 'Development plan'),
      ],
      note: text(
        '点击要点可以查看具体建议和参考依据，也可以导出 Word 文档用于面谈准备。',
        'Open any key point to see detailed guidance and supporting references, or export a Word document for preparation.',
      ),
      meaning: text(
        '在正式表达前提前检查谈话结构、事实依据、员工可能的反应和后续安排，减少临场遗漏与表达失误。',
        'Check the conversation structure, evidence, possible employee reactions, and follow-up before speaking, reducing omissions and avoidable wording mistakes.',
      ),
    },
    {
      id: 'rehearsal',
      title: text('多轮预演', 'Multi-turn rehearsal'),
      guide: guide(
        '通过文字或语音输入经理的话语，与模拟员工进行多轮沟通。根据员工的回复和情绪变化调整表达，必要时可以补充临时背景设定。',
        'Use text or voice to conduct a multi-turn conversation with a simulated employee. Adjust your wording to the employee\'s responses and emotional changes, and add temporary context when needed.',
        '把“知道应该怎么说”转化为“能够在真实反应下说清楚”。你可以安全地尝试不同表达，观察员工可能产生的情绪、质疑和防御，并练习如何继续推进对话。',
        'Turn knowing what to say into being able to say it clearly in response to realistic reactions. Safely test different wording, observe possible emotions, questions, and defensiveness, and practice moving the conversation forward.',
      ),
      action: [text(
        '通过文字或语音输入经理的话语，与模拟员工进行多轮沟通。根据员工的回复和情绪变化调整表达，必要时可以补充临时背景设定。',
        'Use text or voice to conduct a multi-turn conversation with a simulated employee. Adjust your wording to the employee\'s responses and emotional changes, and add temporary context when needed.',
      )],
      note: text(
        '右侧四个维度会提示当前对话已经实质涉及的部分，但它们不是最终评分。',
        'The four dimensions on the right indicate what the conversation has substantively covered; they are not final scores.',
      ),
      meaning: text(
        '把“知道应该怎么说”转化为“能够在真实反应下说清楚”。你可以安全地尝试不同表达，观察员工可能产生的情绪、质疑和防御，并练习如何继续推进对话。',
        'Turn knowing what to say into being able to say it clearly in response to realistic reactions. Safely test different wording, observe possible emotions, questions, and defensiveness, and practice moving the conversation forward.',
      ),
    },
    {
      id: 'report',
      title: text('复盘报告', 'Review report'),
      guide: guide(
        '结束预演后，查看四个维度的评分、评分依据、有待改进之处、建议表达和员工情绪轨迹，并导出完整报告。',
        'After the rehearsal, review scores, evidence, improvement areas, suggested wording, and the employee emotion trajectory across four dimensions, then export the complete report.',
        '帮助你识别哪些表达已经有效，哪些地方仍然模糊、缺少依据或未能承接员工反应，并把问题转化为下一次可以直接练习的具体话术。',
        'Identify what already worked and what remains unclear, unsupported, or unresponsive to the employee, then turn each issue into specific language you can practice next time.',
      ),
      action: [text(
        '结束预演后，查看四个维度的评分、评分依据、有待改进之处、建议表达和员工情绪轨迹，并导出完整报告。',
        'After the rehearsal, review scores, evidence, improvement areas, suggested wording, and the employee emotion trajectory across four dimensions, then export the complete report.',
      )],
      meaning: text(
        '帮助你识别哪些表达已经有效，哪些地方仍然模糊、缺少依据或未能承接员工反应，并把问题转化为下一次可以直接练习的具体话术。',
        'Identify what already worked and what remains unclear, unsupported, or unresponsive to the employee, then turn each issue into specific language you can practice next time.',
      ),
    },
  ],
} satisfies AgentIntroductionContent;

const journeyNumbers = {
  profile: '1',
  intent: '2.1',
  simulation: '3',
  guidance: '4',
  rehearsal: '5',
  report: '6',
} satisfies Record<StepKey, string>;

export function getAgentIntroductionJourneySteps(
  content: AgentIntroductionContent = agentIntroductionContent,
): readonly AgentJourneyDisplayStep[] {
  const result: AgentJourneyDisplayStep[] = [];

  content.steps.forEach((step) => {
    if (step.id !== 'intent') {
      result.push({
        id: step.id,
        number: journeyNumbers[step.id],
        title: step.title,
        guide: step.guide,
        previewId: step.id,
      });
      return;
    }

    result.push({
      id: 'intent',
      number: journeyNumbers.intent,
      title: step.title,
      guide: step.guide,
      previewId: 'intent',
    });

    if (!step.performanceGuide) {
      throw new Error('The intent introduction requires performanceGuide content.');
    }
    result.push({
      id: 'intent-performance',
      number: '2.2',
      title: content.journey.performanceTitle,
      guide: step.performanceGuide,
      previewId: 'intentPerformance',
    });
  });

  return result;
}

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import { agentIntroductionContent } from '../src/content/agent-introduction-content.ts';
import type { StepKey } from '../src/types/domain.ts';
import { calculateWorkflowGuideSpotlight } from '../src/utils/workflowGuideGeometry.ts';
import {
  WORKFLOW_GUIDE_VERSION,
  dismissWorkflowGuidePage,
  emptyWorkflowGuideState,
  readWorkflowGuideState,
  resetWorkflowGuidePage,
  workflowGuideStorageKey,
  writeWorkflowGuideState,
  type WorkflowGuidePageKey,
  type WorkflowGuideStorage,
} from '../src/utils/workflowGuideStorage.ts';

class MemoryStorage implements WorkflowGuideStorage {
  readonly values = new Map<string, string>();

  getItem(key: string): string | null {
    return this.values.get(key) ?? null;
  }

  setItem(key: string, value: string): void {
    this.values.set(key, value);
  }
}

const WORKFLOW_PAGES: readonly StepKey[] = [
  'profile',
  'intent',
  'simulation',
  'guidance',
  'rehearsal',
  'report',
];

const EXPECTED_GUIDES: Record<WorkflowGuidePageKey, readonly [string, string]> = {
  profile: [
    '选择本次沟通的员工，核对其部门、岗位、职级、绩效与工作目标，并补充系统尚未包含的背景信息。',
    '让后续建议建立在真实员工资料和工作目标上，减少笼统判断，避免生成与员工实际岗位无关的内容。',
  ],
  intent: [
    '选择本次沟通的主要方向：发展 \\ 改进 \\ 退出 \\ 发展与改进 \\ 改进与退出预警',
    '同一项绩效结果，在不同沟通意图下需要不同的重点、边界和表达方式。明确意图可以避免对话偏离目的。',
  ],
  'intent-performance': [
    '系统会结合员工目标、岗位、职级和绩效信息，推演员工当前可能的表现。你需要核对并编辑这些内容，确保它符合你掌握的事实。',
    '同一项绩效结果，在不同沟通意图下需要不同的重点、边界和表达方式。明确意图可以避免对话偏离目的，也不会让模型替你作出正式管理决定。',
  ],
  simulation: [
    '设置员工的大五人格倾向，选择一项主诉求，并补充最多两项辅诉求。',
    '人格决定员工可能采用怎样的措辞、语气和互动姿态；诉求决定员工在对话中真正关注什么。它们共同帮助预演呈现更贴近真实员工的反应，但不会改变员工资料和绩效事实。',
  ],
  guidance: [
    '查看并筛选四个维度的准备建议：- 开场定调 - 情绪承接 - 产出与标准 - 发展计划。点击要点可以查看具体建议和参考依据，也可以导出 Word 文档用于面谈准备。',
    '在正式表达前提前检查谈话结构、事实依据、员工可能的反应和后续安排，减少临场遗漏与表达失误。',
  ],
  rehearsal: [
    '通过文字或语音输入经理的话语，与模拟员工进行多轮沟通。根据员工的回复和情绪变化调整表达，必要时可以补充临时背景设定。',
    '把“知道应该怎么说”转化为“能够在真实反应下说清楚”。你可以安全地尝试不同表达，观察员工可能产生的情绪、质疑和防御，并练习如何继续推进对话。',
  ],
  report: [
    '结束预演后，查看四个维度的评分、评分依据、有待改进之处、建议表达和员工情绪轨迹，并导出完整报告。',
    '帮助你识别哪些表达已经有效，哪些地方仍然模糊、缺少依据或未能承接员工反应，并把问题转化为下一次可以直接练习的具体话术。',
  ],
};

test('defines one shared introduction for every workflow page', () => {
  assert.deepEqual(agentIntroductionContent.steps.map(({ id }) => id), WORKFLOW_PAGES);
  const actualGuides: Record<string, readonly [string, string]> = {};

  for (const introduction of agentIntroductionContent.steps) {
    assert.ok(introduction.title.zh.trim().length > 0);
    assert.ok(introduction.title.en.trim().length > 0);
    assert.ok(introduction.guide.task.zh.trim().length > 0);
    assert.ok(introduction.guide.task.en.trim().length > 0);
    assert.ok(introduction.guide.meaning.zh.trim().length > 0);
    assert.ok(introduction.guide.meaning.en.trim().length > 0);
    actualGuides[introduction.id] = [
      introduction.guide.task.zh,
      introduction.guide.meaning.zh,
    ];
    if (introduction.performanceGuide) {
      actualGuides['intent-performance'] = [
        introduction.performanceGuide.task.zh,
        introduction.performanceGuide.meaning.zh,
      ];
    }
    assert.ok(introduction.action.length > 0);
    assert.ok(introduction.meaning.zh.trim().length > 0);
    assert.ok(introduction.meaning.en.trim().length > 0);
    assert.equal('anchor' in introduction, false);
    assert.equal('finishesPage' in introduction, false);
  }
  assert.deepEqual(actualGuides, EXPECTED_GUIDES);
});

test('shows task and meaning beside the correct title with a dismissing spotlight', () => {
  const source = readFileSync(
    new URL('../src/components/WorkflowPageGuide.tsx', import.meta.url),
    'utf8',
  );

  const intentSource = readFileSync(
    new URL('../src/pages/steps/IntentStep.tsx', import.meta.url),
    'utf8',
  );
  const styles = readFileSync(
    new URL('../src/styles/workflow-guide.css', import.meta.url),
    'utf8',
  );

  assert.match(source, /new URLSearchParams\(search\)\.get\('stage'\) === 'performance'/);
  assert.match(source, /key: 'intent-performance'/);
  assert.match(source, /hostSelector: '#screen-intent > \.page-intro'/);
  assert.match(source, /headingSelector: 'h1'/);
  assert.match(source, /createPortal\([\s\S]*?titleHost/);
  assert.match(source, /className="workflow-page-guide-task"[\s\S]*?guideCopy\.task/);
  assert.match(source, /className="workflow-page-guide-meaning"[\s\S]*?guideCopy\.meaning/);
  assert.match(source, /className=\{`workflow-page-guide-dismiss-surface\$\{isExiting/);
  assert.match(source, /onClick=\{dismiss\}/);
  assert.match(
    source,
    /workflow-page-guide-meaning[\s\S]*?workflow-page-guide-skip-hint[\s\S]*?<\/aside>/,
  );
  assert.doesNotMatch(
    source,
    /workflow-page-guide-dismiss-surface[\s\S]{0,400}workflow-page-guide-skip-hint/,
  );
  assert.match(source, /点击任意位置跳过教学/);
  assert.match(source, /Click anywhere to skip/);
  assert.match(source, /const overlay = spotlightVisible && spotlightRect/);
  assert.match(source, /useLayoutEffect\(\(\) => \{[\s\S]*?classList\.add\('workflow-page-guide-host'\)/);
  assert.match(source, /getBoundingClientRect\(\)/);
  assert.match(source, /findNextContentBoundary\(titleHost\)/);
  assert.match(source, /const mutationRoot = titleHost\.parentElement/);
  assert.match(source, /new MutationObserver\(scheduleSpotlightUpdate\)/);
  assert.match(source, /if \(!active \|\| frame\) return;[\s\S]*?frame = 0;/);
  assert.match(source, /observe\(mutationRoot, \{ childList: true \}\)/);
  assert.match(source, /addEventListener\('scroll', scheduleSpotlightUpdate, \{ passive: true \}\)/);
  assert.match(source, /calculateWorkflowGuideSpotlight\(\{/);
  assert.match(source, /resizeObserver\?\.observe\(titleHost\)/);
  assert.match(source, /resizeObserver\?\.observe\(contentBoundary\)/);
  assert.doesNotMatch(source, /scrollHeight|--workflow-guide-expanded-height|handleGuideTransitionEnd/);
  assert.match(source, /className=\{`workflow-page-guide-spotlight\$\{isActive/);
  assert.match(source, /if \(!guidePageKey \|\| isExiting\) return;/);
  assert.match(source, /const presented = visible \|\| isExiting;/);
  assert.match(source, /className=\{`workflow-page-guide\$\{presented \? ' is-presented' : ' is-idle'\}/);
  assert.match(source, /aria-hidden=\{!isActive\}/);
  assert.match(source, /window\.requestAnimationFrame\(\(\) => \{[\s\S]*?setEnteredVisitKey\(visitKey\)/);
  assert.match(source, /window\.setTimeout\(finishExit, GUIDE_EXIT_FALLBACK_MS\)/);
  assert.match(source, /event\.propertyName !== 'opacity'[\s\S]*?finishExit\(\)/);
  assert.match(source, /prefers-reduced-motion: reduce/);
  assert.match(source, /agentIntroductionContent\.steps\.find/);
  assert.match(source, /isWorkflowPageGuideAlwaysShow/);
  assert.match(source, /alwaysShow \? window\.sessionStorage : window\.localStorage/);
  assert.match(source, /const guideDismissed = Boolean\(guidePageKey && guideState\.pages\[guidePageKey\]\?\.dismissed\)/);
  assert.match(source, /previousGuidePageRef[\s\S]*?previous\.pageKey !== guidePageKey[\s\S]*?dismissWorkflowGuidePage\(current, previous\.pageKey\)/);
  assert.match(source, /readyVisitKey === visitKey/);
  assert.doesNotMatch(source, /dismissedVisitKey/);
  assert.match(
    intentSource,
    /<h1>\{view === 'performance'[\s\S]*?translate\('员工表现', 'Employee performance'\)[\s\S]*?: translate\('沟通意图', 'Conversation intent'\)\}<\/h1>/,
  );
  assert.doesNotMatch(intentSource, /data-workflow-guide-host="intent-performance"/);
  assert.match(styles, /\.workflow-page-guide\s*>\s*\.workflow-page-guide-task\s*\{[\s\S]*?color:\s*#1d1d1f;[\s\S]*?font-weight:\s*700;/);
  assert.match(styles, /\.workflow-page-guide\s*>\s*\.workflow-page-guide-meaning\s*\{[\s\S]*?color:\s*#6e6e73;[\s\S]*?font-weight:\s*400;/);
  assert.match(styles, /\.workflow-page-guide-spotlight\s*\{[\s\S]*?position:\s*fixed;[\s\S]*?bottom:\s*0;[\s\S]*?background:\s*rgba\(5, 28, 44, \.68\);[\s\S]*?box-shadow:\s*none;/);
  assert.doesNotMatch(styles, /(?:backdrop-)?filter\s*:|mix-blend-mode\s*:/);
  assert.match(styles, /\.workflow-page-guide\.is-presented\s*\{[\s\S]*?display:\s*grid;[\s\S]*?visibility:\s*visible;/);
  assert.doesNotMatch(styles, /max-height|100vmax/);
  assert.match(styles, /\.workflow-page-guide-spotlight\.is-active\s*\{[\s\S]*?opacity:\s*1;/);
  assert.match(styles, /\.workflow-page-guide-spotlight\.is-exiting\s*\{[\s\S]*?opacity:\s*0;/);
  assert.match(styles, /\.workflow-page-guide\.is-active\s*>\s*\.workflow-page-guide-meaning\s*\{[\s\S]*?transition-delay:\s*60ms;/);
  assert.match(styles, /@keyframes\s+workflowGuideHintPulse/);
  assert.match(
    styles,
    /\.workflow-page-guide-skip-hint::before\s*\{[\s\S]*?background:\s*#051c2c;[\s\S]*?animation:\s*workflowGuideHintPulse\s+2400ms/,
  );
  assert.doesNotMatch(styles, /will-change\s*:/);
  assert.doesNotMatch(styles, /transition\s*:\s*all|transition-property:\s*[^;]*(?:top|left|width|height)/);
  assert.match(styles, /\.workflow-page-guide-dismiss-surface\s*\{[\s\S]*?position:\s*fixed;[\s\S]*?inset:\s*0;/);
  assert.match(styles, /\.workflow-page-guide-skip-hint\s*\{[\s\S]*?grid-column:\s*1;[\s\S]*?grid-row:\s*3;[\s\S]*?pointer-events:\s*none;/);
  assert.doesNotMatch(styles, /\.workflow-page-guide-skip-hint\s*\{[^}]*position:\s*fixed;/);
  assert.match(styles, /\.workflow-page-guide-skip-hint\s*\{[\s\S]*?background:\s*transparent;[\s\S]*?color:\s*#051c2c;/);
  assert.match(styles, /\.workflow-page-guide\.is-active\s*>\s*\.workflow-page-guide-skip-hint\s*\{[\s\S]*?opacity:\s*1;/);
  const hostRules = [...styles.matchAll(/\.workflow-page-guide-host\s*\{([^}]*)\}/g)];
  assert.ok(hostRules.length >= 3);
  hostRules.forEach(([, declarations]) => {
    assert.doesNotMatch(declarations, /align-items\s*:/);
  });
  assert.match(
    styles,
    /\.workflow-page-guide-host\s*>\s*h1,\s*\.workflow-page-guide-host\s*>\s*h3\s*\{[^}]*align-self:\s*start;/,
  );
  assert.match(styles, /white-space:\s*normal;/);
  assert.doesNotMatch(styles, /text-overflow:\s*ellipsis/);
  assert.doesNotMatch(
    styles,
    /workflow-page-guide-(?:task|meaning)[^{]*\{[^}]*white-space:\s*nowrap/,
  );
  assert.doesNotMatch(
    source,
    /role="dialog"|aria-modal|workflow-page-guide-card|workflow-page-guide-hint|guideLines|introduction\.options|action\.slice|introduction\.note|findTarget|anchorSelector|workflow-guide-target|completeWorkflowGuideStep/,
  );
});

test('keeps the original-color spotlight full width and above the next module', () => {
  assert.deepEqual(calculateWorkflowGuideSpotlight({
    viewportWidth: 1440,
    viewportHeight: 900,
    focusBottom: 150.2,
    nextModuleTop: 212.8,
  }), {
    top: 0,
    left: 0,
    width: 1440,
    height: 212,
  });

  assert.deepEqual(calculateWorkflowGuideSpotlight({
    viewportWidth: 1440,
    viewportHeight: 900,
    focusBottom: 150,
    nextModuleTop: 1200,
  }), {
    top: 0,
    left: 0,
    width: 1440,
    height: 900,
  });

  assert.deepEqual(calculateWorkflowGuideSpotlight({
    viewportWidth: 390,
    viewportHeight: 844,
    focusBottom: 160.2,
    nextModuleTop: 120,
  }), {
    top: 0,
    left: 0,
    width: 390,
    height: 120,
  });

  assert.deepEqual(calculateWorkflowGuideSpotlight({
    viewportWidth: 390,
    viewportHeight: 844,
    focusBottom: 160.2,
  }), {
    top: 0,
    left: 0,
    width: 390,
    height: 161,
  });
});

test('stores page dismissal independently for each account and page', () => {
  const storage = new MemoryStorage();
  const firstUser = 'Manager.One@bosch.com';
  const secondUser = 'manager.two@bosch.com';

  const state = dismissWorkflowGuidePage(emptyWorkflowGuideState(), 'profile');
  assert.equal(writeWorkflowGuideState(storage, firstUser, state), true);

  assert.deepEqual(readWorkflowGuideState(storage, firstUser).pages.profile, {
    dismissed: true,
  });
  assert.equal(readWorkflowGuideState(storage, firstUser).pages.intent, undefined);
  assert.deepEqual(readWorkflowGuideState(storage, secondUser), emptyWorkflowGuideState());
  assert.notEqual(workflowGuideStorageKey(firstUser), workflowGuideStorageKey(secondUser));
  assert.equal(
    workflowGuideStorageKey('  MANAGER.ONE@BOSCH.COM  '),
    workflowGuideStorageKey(firstUser),
  );
  assert.match(workflowGuideStorageKey(firstUser), /:workflow-guide:v3:/);
});

test('intent selection and employee performance dismissal stay independent', () => {
  let state = dismissWorkflowGuidePage(emptyWorkflowGuideState(), 'profile');
  state = dismissWorkflowGuidePage(state, 'intent');
  state = dismissWorkflowGuidePage(state, 'intent-performance');

  const reset = resetWorkflowGuidePage(state, 'intent-performance');

  assert.equal(reset.pages['intent-performance'], undefined);
  assert.equal(reset.pages.intent?.dismissed, true);
  assert.equal(reset.pages.profile?.dismissed, true);
  assert.equal(state.pages['intent-performance']?.dismissed, true, 'updates remain immutable');
});

test('invalid or stale browser data is ignored safely', () => {
  const storage = new MemoryStorage();
  storage.values.set(workflowGuideStorageKey('manager'), '{not-json');
  assert.deepEqual(readWorkflowGuideState(storage, 'manager'), emptyWorkflowGuideState());

  storage.values.set(workflowGuideStorageKey('manager'), JSON.stringify({
    version: WORKFLOW_GUIDE_VERSION - 1,
    pages: { profile: { dismissed: true } },
  }));
  assert.deepEqual(readWorkflowGuideState(storage, 'manager'), emptyWorkflowGuideState());

  storage.values.set(workflowGuideStorageKey('manager'), JSON.stringify({
    version: WORKFLOW_GUIDE_VERSION,
    pages: {
      profile: { dismissed: 'yes' },
      intent: { dismissed: true },
      'intent-performance': { dismissed: true },
      unknown: { dismissed: true },
    },
  }));
  assert.deepEqual(readWorkflowGuideState(storage, 'manager').pages, {
    intent: { dismissed: true },
    'intent-performance': { dismissed: true },
  });
});

test('storage failures safely fall back without blocking the workflow', () => {
  const storage: WorkflowGuideStorage = {
    getItem: () => { throw new Error('storage disabled'); },
    setItem: () => { throw new Error('quota exceeded'); },
  };

  assert.deepEqual(readWorkflowGuideState(storage, 'manager'), emptyWorkflowGuideState());
  assert.equal(writeWorkflowGuideState(storage, 'manager', emptyWorkflowGuideState()), false);
});

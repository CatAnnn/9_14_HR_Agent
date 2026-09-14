import assert from 'node:assert/strict';
import test from 'node:test';

import {
  agentIntroductionContent,
  getAgentIntroductionJourneySteps,
} from '../src/content/agent-introduction-content.ts';
import {
  buildJourneyRouteGeometry,
  calculateAgentJourneyProgress,
  calculateJourneyActiveMarkerY,
  calculateJourneyRouteMarkerY,
  interpolateJourneyRouteProgress,
  synchronizeJourneyRouteDistance,
} from '../src/hooks/useAgentJourneyProgress.ts';

const expectedStepKeys = [
  'profile',
  'intent',
  'simulation',
  'guidance',
  'rehearsal',
  'report',
];

const expectedPreviewKeys = [
  'profile',
  'intent',
  'intentPerformance',
  'simulation',
  'guidance',
  'rehearsal',
  'report',
];

const expectedJourneyStepIds = [
  'profile',
  'intent',
  'intent-performance',
  'simulation',
  'guidance',
  'rehearsal',
  'report',
];

test('journey content and synthetic preview registry cover all workflow steps', () => {
  const contentStepKeys = agentIntroductionContent.steps.map(({ id }) => id);
  const previewStepKeys = Object.keys(agentIntroductionContent.journey.previews);

  assert.deepEqual(contentStepKeys, expectedStepKeys);
  assert.deepEqual(previewStepKeys, expectedPreviewKeys);
  assert.equal(new Set(contentStepKeys).size, expectedStepKeys.length);
});

test('journey presents intent and employee performance as separate 2.1 and 2.2 nodes', () => {
  const steps = getAgentIntroductionJourneySteps();

  assert.deepEqual(steps.map(({ id }) => id), expectedJourneyStepIds);
  assert.deepEqual(steps.map(({ number }) => number), ['1', '2.1', '2.2', '3', '4', '5', '6']);
  assert.equal(steps[1]?.previewId, 'intent');
  assert.equal(steps[2]?.previewId, 'intentPerformance');
  assert.deepEqual(
    steps[2]?.guide,
    agentIntroductionContent.steps.find(({ id }) => id === 'intent')?.performanceGuide,
  );
});

test('journey content keeps a bilingual task and meaning for every step', () => {
  agentIntroductionContent.steps.forEach((step) => {
    for (const localized of [step.title, step.guide.task, step.guide.meaning]) {
      assert.ok(localized.zh.trim());
      assert.ok(localized.en.trim());
    }
  });
});

test('scroll progress advances and rolls back deterministically', () => {
  const centers = [100, 300, 500];

  assert.deepEqual(
    calculateAgentJourneyProgress(centers, 50),
    { activeIndex: -1, segmentProgress: [0, 0], routeProgress: 50 / 600 },
  );
  assert.deepEqual(
    calculateAgentJourneyProgress(centers, 200),
    { activeIndex: 0, segmentProgress: [0.5, 0], routeProgress: 200 / 600 },
  );
  assert.deepEqual(
    calculateAgentJourneyProgress(centers, 350),
    { activeIndex: 1, segmentProgress: [1, 0.25], routeProgress: 350 / 600 },
  );
  assert.deepEqual(
    calculateAgentJourneyProgress(centers, 600),
    { activeIndex: 2, segmentProgress: [1, 1], routeProgress: 1 },
  );
  assert.deepEqual(
    calculateAgentJourneyProgress(centers, 200),
    { activeIndex: 0, segmentProgress: [0.5, 0], routeProgress: 200 / 600 },
  );
});

test('empty journey progress is stable', () => {
  assert.deepEqual(
    calculateAgentJourneyProgress([], 100),
    { activeIndex: -1, segmentProgress: [], routeProgress: 0 },
  );
});

test('route drawing leads the reading marker within the visible viewport', () => {
  assert.equal(calculateJourneyActiveMarkerY(800), 416);
  assert.equal(calculateJourneyRouteMarkerY(800), 656);
  assert.equal(calculateJourneyRouteMarkerY(300), 236);
  assert.ok(calculateJourneyRouteMarkerY(800) > calculateJourneyActiveMarkerY(800));
  assert.equal(calculateJourneyActiveMarkerY(0), 0);
  assert.equal(calculateJourneyRouteMarkerY(Number.NaN), 0);
});

test('route geometry draws from its origin and gives horizontal travel a smaller budget', () => {
  const geometry = buildJourneyRouteGeometry([
    { x: 0, y: 0 },
    { x: 100, y: 0 },
    { x: 100, y: 100 },
  ], 200);
  assert.ok(geometry);
  assert.equal(geometry.anchorYs[0], 0);
  assert.equal(geometry.anchorYs[2], 200);
  assert.equal(geometry.anchorProgress[0], 0);
  assert.equal(geometry.anchorProgress[2], 1);
  assert.ok(geometry.anchorYs[1] > 15);
  assert.ok(geometry.anchorYs[1] < 30);
  assert.equal(
    interpolateJourneyRouteProgress(
      geometry.anchorYs,
      geometry.anchorProgress,
      geometry.anchorYs[1],
    ),
    0.5,
  );
  assert.equal(buildJourneyRouteGeometry([{ x: 0, y: 0 }], 200), null);
});

test('long horizontal turns use a bounded scroll budget on wide layouts', () => {
  const points = [
    ...Array.from({ length: 13 }, (_, index) => ({ x: index * 100, y: 0 })),
    ...Array.from({ length: 4 }, (_, index) => ({ x: 1200, y: (index + 1) * 100 })),
  ];
  const geometry = buildJourneyRouteGeometry(points, 400, 0.45);
  assert.ok(geometry);
  assert.ok(geometry.anchorYs[12] < 60);
  assert.equal(geometry.anchorProgress[12], 0.75);
});

test('route distance follows every scroll target without temporal lag', () => {
  assert.equal(synchronizeJourneyRouteDistance(800, 0), 800);
  assert.equal(synchronizeJourneyRouteDistance(0, 800), 0);
  assert.equal(synchronizeJourneyRouteDistance(245.75, 100), 245.75);
  assert.equal(synchronizeJourneyRouteDistance(Number.NaN, 120), 120);
  assert.equal(synchronizeJourneyRouteDistance(Number.NaN, Number.NaN), 0);
});

test('route geometry pins every label to its own scroll anchor', () => {
  const geometry = buildJourneyRouteGeometry([
    { x: 0, y: 0 },
    { x: 100, y: 0 },
    { x: 100, y: 100 },
    { x: 200, y: 100 },
    { x: 200, y: 200 },
  ], 200, 0.12, [
    { pointIndex: 0, markerY: 0 },
    { pointIndex: 2, markerY: 100 },
    { pointIndex: 4, markerY: 200 },
  ]);
  assert.ok(geometry);
  assert.equal(geometry.anchorYs[2], 100);
  assert.deepEqual(geometry.labelProgress, [0.5]);
  assert.equal(interpolateJourneyRouteProgress(
    geometry.anchorYs,
    geometry.anchorProgress,
    100,
  ), 0.5);
});

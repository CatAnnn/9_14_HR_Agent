import {
  useCallback,
  useLayoutEffect,
  useRef,
  useState,
} from 'react';

export interface AgentJourneyProgress {
  readonly activeIndex: number;
  readonly segmentProgress: readonly number[];
  readonly routeProgress: number;
}

const clampUnit = (value: number): number => Math.min(1, Math.max(0, value));
const JOURNEY_ACTIVE_MARKER_RATIO = 0.52;
const JOURNEY_ROUTE_MARKER_RATIO = 0.82;
const JOURNEY_ROUTE_BOTTOM_INSET = 64;
const JOURNEY_ROUTE_HORIZONTAL_COST = 0.12;
const JOURNEY_ROUTE_DESKTOP_HORIZONTAL_COST = 0.45;
const JOURNEY_ROUTE_MOBILE_HORIZONTAL_COST = 0.35;
const JOURNEY_ROUTE_MIN_SAMPLES = 256;
const JOURNEY_ROUTE_MAX_SAMPLES = 1536;
const JOURNEY_ROUTE_SAMPLE_LENGTH = 10;
const JOURNEY_ROUTE_MAX_HORIZONTAL_COST_PER_SPAN = 64;
const JOURNEY_ROUTE_MAX_MEASURE_RETRIES = 12;

export function calculateJourneyActiveMarkerY(viewportHeight: number): number {
  if (!Number.isFinite(viewportHeight) || viewportHeight <= 0) return 0;
  return viewportHeight * JOURNEY_ACTIVE_MARKER_RATIO;
}

export function calculateJourneyRouteMarkerY(viewportHeight: number): number {
  if (!Number.isFinite(viewportHeight) || viewportHeight <= 0) return 0;
  return Math.max(0, Math.min(
    viewportHeight * JOURNEY_ROUTE_MARKER_RATIO,
    viewportHeight - JOURNEY_ROUTE_BOTTOM_INSET,
  ));
}

export interface JourneyRouteGeometry {
  readonly anchorYs: readonly number[];
  readonly anchorProgress: readonly number[];
  readonly labelProgress: readonly number[];
}

export interface JourneyRoutePoint {
  readonly x: number;
  readonly y: number;
}

export interface JourneyRouteAnchor {
  readonly pointIndex: number;
  readonly markerY: number;
}

export function buildJourneyRouteGeometry(
  points: readonly JourneyRoutePoint[],
  routeHeight: number,
  horizontalCost = JOURNEY_ROUTE_HORIZONTAL_COST,
  routeAnchors?: readonly JourneyRouteAnchor[],
): JourneyRouteGeometry | null {
  if (
    points.length < 2
    || !Number.isFinite(routeHeight)
    || routeHeight <= 0
    || !Number.isFinite(horizontalCost)
    || horizontalCost < 0
  ) return null;

  const verticalCosts = [0];
  const horizontalDistances = [0];
  let totalCost = 0;
  for (let index = 1; index < points.length; index += 1) {
    const previous = points[index - 1];
    const current = points[index];
    if (
      !Number.isFinite(previous.x)
      || !Number.isFinite(previous.y)
      || !Number.isFinite(current.x)
      || !Number.isFinite(current.y)
    ) return null;
    const verticalCost = Math.abs(current.y - previous.y);
    const horizontalDistance = Math.abs(current.x - previous.x);
    verticalCosts.push(verticalCost);
    horizontalDistances.push(horizontalDistance);
    totalCost += verticalCost + (horizontalDistance * horizontalCost);
  }
  if (totalCost <= 0) return null;

  const lastIndex = points.length - 1;
  const anchors = routeAnchors && routeAnchors.length >= 2
    ? routeAnchors
        .map((anchor) => ({
          pointIndex: Math.max(0, Math.min(lastIndex, Math.round(anchor.pointIndex))),
          markerY: Math.max(0, Math.min(routeHeight, anchor.markerY)),
        }))
        .filter((anchor, index, entries) => (
          index === 0
          || (
            anchor.pointIndex > entries[index - 1].pointIndex
            && anchor.markerY >= entries[index - 1].markerY
          )
        ))
    : [
        { pointIndex: 0, markerY: 0 },
        { pointIndex: lastIndex, markerY: routeHeight },
      ];
  if (anchors[0]?.pointIndex !== 0) {
    anchors.unshift({ pointIndex: 0, markerY: 0 });
  }
  if (anchors[anchors.length - 1]?.pointIndex !== lastIndex) {
    anchors.push({ pointIndex: lastIndex, markerY: routeHeight });
  }

  const mappedMarkerYs = new Array<number>(points.length).fill(0);
  for (let anchorIndex = 1; anchorIndex < anchors.length; anchorIndex += 1) {
    const start = anchors[anchorIndex - 1];
    const end = anchors[anchorIndex];
    let horizontalDistance = 0;
    for (let pointIndex = start.pointIndex + 1; pointIndex <= end.pointIndex; pointIndex += 1) {
      horizontalDistance += horizontalDistances[pointIndex];
    }
    const effectiveHorizontalCost = horizontalDistance <= 0
      ? 0
      : Math.min(
          horizontalCost,
          JOURNEY_ROUTE_MAX_HORIZONTAL_COST_PER_SPAN / horizontalDistance,
        );

    const spanCosts = [0];
    let costSpan = 0;
    for (let pointIndex = start.pointIndex + 1; pointIndex <= end.pointIndex; pointIndex += 1) {
      costSpan += (
        verticalCosts[pointIndex]
        + (horizontalDistances[pointIndex] * effectiveHorizontalCost)
      );
      spanCosts.push(costSpan);
    }
    for (let pointIndex = start.pointIndex; pointIndex <= end.pointIndex; pointIndex += 1) {
      const fraction = costSpan <= 0
        ? (pointIndex - start.pointIndex) / Math.max(1, end.pointIndex - start.pointIndex)
        : spanCosts[pointIndex - start.pointIndex] / costSpan;
      mappedMarkerYs[pointIndex] = (
        start.markerY + ((end.markerY - start.markerY) * clampUnit(fraction))
      );
    }
  }

  return {
    anchorYs: mappedMarkerYs,
    anchorProgress: points.map((_, index) => index / lastIndex),
    labelProgress: anchors.slice(1, -1).map((anchor) => anchor.pointIndex / lastIndex),
  };
}

export function synchronizeJourneyRouteDistance(
  targetDistance: number,
  previousDistance = 0,
): number {
  if (Number.isFinite(targetDistance)) return Math.max(0, targetDistance);
  return Number.isFinite(previousDistance) ? Math.max(0, previousDistance) : 0;
}

export function interpolateJourneyRouteProgress(
  anchorYs: readonly number[],
  anchorProgress: readonly number[],
  markerY: number,
): number {
  if (anchorYs.length < 2 || anchorYs.length !== anchorProgress.length) return 0;
  if (markerY <= anchorYs[0]) return clampUnit(anchorProgress[0]);
  const lastIndex = anchorYs.length - 1;
  if (markerY >= anchorYs[lastIndex]) return clampUnit(anchorProgress[lastIndex]);

  let low = 1;
  let high = lastIndex;
  while (low < high) {
    const middle = Math.floor((low + high) / 2);
    if (markerY <= anchorYs[middle]) high = middle;
    else low = middle + 1;
  }

  const endIndex = low;
  const startIndex = endIndex - 1;
  const startY = anchorYs[startIndex];
  const endY = anchorYs[endIndex];
  const span = endY - startY;
  const fraction = span <= 0 ? 1 : clampUnit((markerY - startY) / span);
  const startProgress = anchorProgress[startIndex];
  return clampUnit(
    startProgress
    + ((anchorProgress[endIndex] - startProgress) * fraction),
  );
}

interface MeasuredJourneyRoute extends JourneyRouteGeometry {
  readonly totalLength: number;
}

function findJourneyRouteAnchors(
  points: readonly JourneyRoutePoint[],
  centerX: number,
  labelYs: readonly number[],
  routeHeight: number,
): JourneyRouteAnchor[] {
  const lastIndex = points.length - 1;
  const anchors: JourneyRouteAnchor[] = [{ pointIndex: 0, markerY: 0 }];
  let previousIndex = 0;

  labelYs.forEach((labelY, labelIndex) => {
    const remainingLabels = labelYs.length - labelIndex - 1;
    const firstCandidate = Math.min(lastIndex, previousIndex + 1);
    const lastCandidate = Math.max(firstCandidate, lastIndex - remainingLabels - 1);
    let bestIndex = firstCandidate;
    let bestDistance = Number.POSITIVE_INFINITY;
    for (let pointIndex = firstCandidate; pointIndex <= lastCandidate; pointIndex += 1) {
      const point = points[pointIndex];
      const distance = ((point.x - centerX) ** 2) + ((point.y - labelY) ** 2);
      if (distance < bestDistance) {
        bestDistance = distance;
        bestIndex = pointIndex;
      }
    }
    anchors.push({ pointIndex: bestIndex, markerY: labelY });
    previousIndex = bestIndex;
  });
  anchors.push({ pointIndex: lastIndex, markerY: routeHeight });
  return anchors;
}

function measureJourneySvgRoute(
  path: SVGPathElement,
  routeHeight: number,
  centerX: number,
  labelYs: readonly number[],
  horizontalCost: number,
): MeasuredJourneyRoute | null {
  try {
    const totalLength = path.getTotalLength();
    if (!Number.isFinite(totalLength) || totalLength <= 0) return null;
    const sampleCount = Math.min(
      JOURNEY_ROUTE_MAX_SAMPLES,
      Math.max(
        JOURNEY_ROUTE_MIN_SAMPLES,
        Math.ceil(totalLength / JOURNEY_ROUTE_SAMPLE_LENGTH),
      ),
    );
    const points = Array.from({ length: sampleCount + 1 }, (_, sampleIndex) => {
      const point = path.getPointAtLength(totalLength * (sampleIndex / sampleCount));
      return { x: point.x, y: point.y };
    });
    const anchors = findJourneyRouteAnchors(points, centerX, labelYs, routeHeight);
    const geometry = buildJourneyRouteGeometry(
      points,
      routeHeight,
      horizontalCost,
      anchors,
    );
    return geometry ? { ...geometry, totalLength } : null;
  } catch {
    return null;
  }
}

function getLayoutTopWithin(
  element: HTMLElement,
  ancestor: HTMLElement,
): number | null {
  let top = 0;
  let current: HTMLElement | null = element;
  while (current && current !== ancestor) {
    top += current.offsetTop;
    current = current.offsetParent as HTMLElement | null;
  }
  return current === ancestor ? top : null;
}

function buildDesktopMeasuredRoutePath(
  width: number,
  height: number,
  labelYs: readonly number[],
): string {
  const left = width * (147 / 1232);
  const right = width * (1085 / 1232);
  const radius = Math.min(24, width * 0.025);
  const parts = [`M${right} -2`];
  let currentX = right;
  let currentY = -2;

  labelYs.forEach((labelY) => {
    const endX = currentX > width / 2 ? left : right;
    const direction = endX > currentX ? 1 : -1;
    const approachY = Math.max(currentY, labelY - radius);
    if (approachY > currentY) parts.push(`V${approachY}`);
    parts.push(
      `Q${currentX} ${labelY} ${currentX + (direction * radius)} ${labelY}`,
      `H${endX - (direction * radius)}`,
      `Q${endX} ${labelY} ${endX} ${labelY + radius}`,
    );
    currentX = endX;
    currentY = labelY + radius;
  });

  if (currentY < height + 2) parts.push(`V${height + 2}`);
  return parts.join(' ');
}

function buildMobileMeasuredRoutePath(
  width: number,
  height: number,
  labelYs: readonly number[],
  previewBottomYs: readonly number[],
): string {
  const center = width / 2;
  const radius = Math.min(24, width * 0.065);
  const parts = [`M${center} -2`];

  labelYs.forEach((labelY, index) => {
    const segmentEnd = labelYs[index + 1] ?? height;
    const direction = index % 2 === 0 ? -1 : 1;
    const sideX = direction < 0 ? -8 : width + 8;
    const turnY = Math.min(labelY + 54, segmentEnd - 96);
    const measuredPreviewBottom = previewBottomYs[index] ?? (labelY + 260);
    const returnY = Math.min(
      segmentEnd - 48,
      Math.max(turnY + 72, measuredPreviewBottom + 28),
    );
    parts.push(
      `V${turnY - radius}`,
      `Q${center} ${turnY} ${center + (direction * radius)} ${turnY}`,
      `H${sideX - (direction * radius)}`,
      `Q${sideX} ${turnY} ${sideX} ${turnY + radius}`,
      `V${returnY - radius}`,
      `Q${sideX} ${returnY} ${sideX - (direction * radius)} ${returnY}`,
      `H${center + (direction * radius)}`,
      `Q${center} ${returnY} ${center} ${returnY + radius}`,
      `V${segmentEnd}`,
    );
  });

  parts.push(`V${height + 2}`);
  return parts.join(' ');
}

export function calculateAgentJourneyProgress(
  stepCenters: readonly number[],
  markerY: number,
): AgentJourneyProgress {
  if (stepCenters.length === 0) {
    return { activeIndex: -1, segmentProgress: [], routeProgress: 0 };
  }

  let activeIndex = -1;
  stepCenters.forEach((center, index) => {
    if (center <= markerY) activeIndex = index;
  });

  const segmentProgress = stepCenters.slice(1).map((end, index) => {
    const start = stepCenters[index];
    if (end <= start) return markerY >= end ? 1 : 0;
    return clampUnit((markerY - start) / (end - start));
  });

  const firstCenter = stepCenters[0];
  const lastCenter = stepCenters[stepCenters.length - 1] ?? firstCenter;
  const firstGap = stepCenters[1] === undefined ? 0 : stepCenters[1] - firstCenter;
  const penultimateCenter = stepCenters[stepCenters.length - 2] ?? lastCenter;
  const lastGap = lastCenter - penultimateCenter;
  const routeStart = firstCenter - firstGap / 2;
  const routeEnd = lastCenter + lastGap / 2;
  const routeProgress = routeEnd <= routeStart
    ? Number(markerY >= routeEnd)
    : clampUnit((markerY - routeStart) / (routeEnd - routeStart));

  return { activeIndex, segmentProgress, routeProgress };
}

export function useAgentJourneyProgress(stepCount: number) {
  const containerRef = useRef<HTMLOListElement>(null);
  const launchRef = useRef<HTMLDivElement | null>(null);
  const launchPathRef = useRef<SVGPathElement | null>(null);
  const stepRefs = useRef<Array<HTMLLIElement | null>>([]);
  const routePathRefs = useRef<Array<SVGPathElement | null>>([]);
  const launchGeometryRef = useRef<MeasuredJourneyRoute | null>(null);
  const launchStartYRef = useRef(0);
  const routeDashOffsetRef = useRef<string[]>(['100', '100']);
  const launchDashOffsetRef = useRef('100');
  const routeGeometryRef = useRef<Array<MeasuredJourneyRoute | null>>([]);
  const routeLabelYsRef = useRef<number[]>([]);
  const visibleRouteIndexRef = useRef(0);
  const geometrySignatureRef = useRef('');
  const geometryDirtyRef = useRef(true);
  const measurementRetryRef = useRef(0);
  const frameRef = useRef<number | null>(null);
  const renderedDistanceRef = useRef(0);
  const [activeIndex, setActiveIndex] = useState(-1);
  const [reachedIndex, setReachedIndex] = useState(-1);

  const setLaunchRef = useCallback((node: HTMLDivElement | null) => {
    launchRef.current = node;
    geometryDirtyRef.current = true;
  }, []);

  const setLaunchPathRef = useCallback((node: SVGPathElement | null) => {
    launchPathRef.current = node;
    launchGeometryRef.current = null;
    geometryDirtyRef.current = true;
    node?.setAttribute('stroke-dashoffset', launchDashOffsetRef.current);
  }, []);

  const setStepRef = useCallback((index: number, node: HTMLLIElement | null) => {
    if (node === null || stepRefs.current[index] === node) return;
    stepRefs.current[index] = node;
    geometryDirtyRef.current = true;
  }, []);

  const setRoutePathRef = useCallback((index: number, node: SVGPathElement | null) => {
    if (node === null) return;
    if (routePathRefs.current[index] !== node) {
      routePathRefs.current[index] = node;
      routeGeometryRef.current[index] = null;
      geometryDirtyRef.current = true;
    }
    node.setAttribute('stroke-dashoffset', routeDashOffsetRef.current[index] ?? '100');
  }, []);

  useLayoutEffect(() => {
    const container = containerRef.current;
    const launch = launchRef.current;
    if (!container || !launch) return undefined;

    const mobileLayoutQuery = window.matchMedia('(max-width: 760px)');

    const measureRouteGeometry = (): 'failed' | 'unchanged' | 'rebuilt' => {
      const measurements = stepRefs.current
        .slice(0, stepCount)
        .map((node) => {
          const label = node?.querySelector<HTMLElement>('.agent-journey-route-label');
          const preview = node?.querySelector<HTMLElement>('.agent-journey-preview-frame');
          if (!label || !preview) return null;
          const labelTop = getLayoutTopWithin(label, container);
          const previewTop = getLayoutTopWithin(preview, container);
          if (labelTop === null || previewTop === null) return null;
          return {
            labelY: labelTop + (label.offsetHeight / 2),
            previewBottomY: previewTop + preview.offsetHeight,
          };
        });
      if (measurements.some((measurement) => measurement === null)) return 'failed';

      const measured = measurements as Array<{
        labelY: number;
        previewBottomY: number;
      }>;
      const measuredLabelYs = measured.map(({ labelY }) => labelY);
      const previewBottomYs = measured.map(({ previewBottomY }) => previewBottomY);
      const width = Math.max(1, container.clientWidth);
      const height = Math.max(1, container.scrollHeight);
      const launchWidth = Math.max(1, launch.clientWidth);
      const launchHeight = Math.max(1, launch.clientHeight);
      const mobileLayout = mobileLayoutQuery.matches;
      const visibleRouteIndex = mobileLayout ? 1 : 0;
      const signature = [
        mobileLayout ? 'mobile' : 'desktop',
        width.toFixed(1),
        height.toFixed(1),
        launchWidth.toFixed(1),
        launchHeight.toFixed(1),
        ...measuredLabelYs.map((value) => value.toFixed(1)),
        ...previewBottomYs.map((value) => value.toFixed(1)),
      ].join(':');
      if (
        signature === geometrySignatureRef.current
        && routeGeometryRef.current[visibleRouteIndex]
        && launchGeometryRef.current
      ) {
        geometryDirtyRef.current = false;
        visibleRouteIndexRef.current = visibleRouteIndex;
        return 'unchanged';
      }

      routeLabelYsRef.current = measuredLabelYs;
      visibleRouteIndexRef.current = visibleRouteIndex;
      const path = routePathRefs.current[visibleRouteIndex];
      if (!path) return 'failed';
      const routePath = mobileLayout
        ? buildMobileMeasuredRoutePath(width, height, measuredLabelYs, previewBottomYs)
        : buildDesktopMeasuredRoutePath(width, height, measuredLabelYs);
      const svg = path.ownerSVGElement;
      svg?.setAttribute('viewBox', `0 0 ${width} ${height}`);
      svg?.querySelector<SVGPathElement>('path.is-base')?.setAttribute('d', routePath);
      path.setAttribute('d', routePath);
      const routeGeometry = measureJourneySvgRoute(
        path,
        height,
        width / 2,
        measuredLabelYs,
        mobileLayout
          ? JOURNEY_ROUTE_MOBILE_HORIZONTAL_COST
          : JOURNEY_ROUTE_DESKTOP_HORIZONTAL_COST,
      );
      if (!routeGeometry) return 'failed';
      routeGeometryRef.current[visibleRouteIndex] = routeGeometry;

      const launchStartY = mobileLayout ? 90 : launchHeight * (35 / 414);
      const launchSpan = Math.max(1, launchHeight - launchStartY + (mobileLayout ? 2 : 0));
      launchStartYRef.current = launchStartY;
      if (mobileLayout) {
        const launchGeometry = buildJourneyRouteGeometry(
          [{ x: 0, y: 0 }, { x: 0, y: launchSpan }],
          launchSpan,
          0,
        );
        if (!launchGeometry) return 'failed';
        launchGeometryRef.current = { ...launchGeometry, totalLength: launchSpan };
      } else {
        const launchPath = launchPathRef.current;
        if (!launchPath) return 'failed';
        launchGeometryRef.current = measureJourneySvgRoute(
          launchPath,
          launchSpan,
          0,
          [],
          JOURNEY_ROUTE_HORIZONTAL_COST,
        );
        if (!launchGeometryRef.current) return 'failed';
      }

      geometrySignatureRef.current = signature;
      geometryDirtyRef.current = false;
      return 'rebuilt';
    };

    const update = () => {
      frameRef.current = null;

      if (geometryDirtyRef.current) {
        const measurementStatus = measureRouteGeometry();
        if (measurementStatus === 'failed') {
          if (measurementRetryRef.current < JOURNEY_ROUTE_MAX_MEASURE_RETRIES) {
            measurementRetryRef.current += 1;
            frameRef.current = window.requestAnimationFrame(update);
          }
          return;
        }
        measurementRetryRef.current = 0;
      }

      const containerRect = container.getBoundingClientRect();
      const launchRect = launch.getBoundingClientRect();
      const activeMarkerY = calculateJourneyActiveMarkerY(window.innerHeight);
      const routeMarkerY = calculateJourneyRouteMarkerY(window.innerHeight);
      const labelCenters = routeLabelYsRef.current.map(
        (labelY) => containerRect.top + labelY,
      );
      const activeProgress = calculateAgentJourneyProgress(labelCenters, activeMarkerY);
      const routeIndex = visibleRouteIndexRef.current;
      const routeGeometry = routeGeometryRef.current[routeIndex];
      const launchGeometry = launchGeometryRef.current;
      if (!routeGeometry || !launchGeometry) return;

      const launchTargetProgress = interpolateJourneyRouteProgress(
        launchGeometry.anchorYs,
        launchGeometry.anchorProgress,
        routeMarkerY - launchRect.top - launchStartYRef.current,
      );
      let routeTargetProgress = interpolateJourneyRouteProgress(
        routeGeometry.anchorYs,
        routeGeometry.anchorProgress,
        routeMarkerY - containerRect.top,
      );
      if (launchTargetProgress < 1) routeTargetProgress = 0;
      const targetDistance = (
        (launchTargetProgress * launchGeometry.totalLength)
        + (routeTargetProgress * routeGeometry.totalLength)
      );
      renderedDistanceRef.current = synchronizeJourneyRouteDistance(
        targetDistance,
        renderedDistanceRef.current,
      );

      const launchProgress = clampUnit(
        renderedDistanceRef.current / launchGeometry.totalLength,
      );
      const routeProgress = clampUnit(
        (renderedDistanceRef.current - launchGeometry.totalLength)
        / routeGeometry.totalLength,
      );
      const launchDashOffset = (100 * (1 - launchProgress)).toFixed(3);
      launchDashOffsetRef.current = launchDashOffset;
      if (
        routeIndex === 0
        && launchPathRef.current?.getAttribute('stroke-dashoffset') !== launchDashOffset
      ) {
        launchPathRef.current?.setAttribute('stroke-dashoffset', launchDashOffset);
      }
      launch.style.setProperty('--journey-launch-progress', launchProgress.toFixed(5));

      const routePath = routePathRefs.current[routeIndex];
      const routeDashOffset = (100 * (1 - routeProgress)).toFixed(3);
      routeDashOffsetRef.current[routeIndex] = routeDashOffset;
      if (routePath?.getAttribute('stroke-dashoffset') !== routeDashOffset) {
        routePath?.setAttribute('stroke-dashoffset', routeDashOffset);
      }

      let nextReachedIndex = -1;
      routeGeometry.labelProgress.forEach((labelProgress, index) => {
        if (routeProgress + Number.EPSILON >= labelProgress) nextReachedIndex = index;
      });
      setActiveIndex((previous) => (
        previous === activeProgress.activeIndex ? previous : activeProgress.activeIndex
      ));
      setReachedIndex((previous) => (
        previous === nextReachedIndex ? previous : nextReachedIndex
      ));
    };

    const schedule = () => {
      if (frameRef.current !== null) return;
      frameRef.current = window.requestAnimationFrame(update);
    };

    const invalidateGeometry = () => {
      geometryDirtyRef.current = true;
      schedule();
    };
    const resizeObserver = typeof ResizeObserver === 'undefined'
      ? null
      : new ResizeObserver(invalidateGeometry);
    resizeObserver?.observe(container);
    resizeObserver?.observe(launch);

    let previousViewportWidth = window.innerWidth;
    const handleViewportResize = () => {
      if (Math.abs(window.innerWidth - previousViewportWidth) >= 1) {
        previousViewportWidth = window.innerWidth;
        geometryDirtyRef.current = true;
      }
      schedule();
    };
    mobileLayoutQuery.addEventListener('change', invalidateGeometry);
    window.addEventListener('scroll', schedule, { passive: true });
    window.addEventListener('resize', handleViewportResize);
    window.visualViewport?.addEventListener('scroll', schedule, { passive: true });
    window.visualViewport?.addEventListener('resize', handleViewportResize);
    geometryDirtyRef.current = true;
    update();

    return () => {
      if (frameRef.current !== null) window.cancelAnimationFrame(frameRef.current);
      resizeObserver?.disconnect();
      mobileLayoutQuery.removeEventListener('change', invalidateGeometry);
      window.removeEventListener('scroll', schedule);
      window.removeEventListener('resize', handleViewportResize);
      window.visualViewport?.removeEventListener('scroll', schedule);
      window.visualViewport?.removeEventListener('resize', handleViewportResize);
    };
  }, [stepCount]);

  return {
    containerRef,
    setLaunchRef,
    setLaunchPathRef,
    setStepRef,
    setRoutePathRef,
    activeIndex,
    reachedIndex,
  };
}

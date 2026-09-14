import { Activity } from 'lucide-react';
import { useLayoutEffect, useRef, useState, type CSSProperties, type KeyboardEvent } from 'react';

import { useLanguage } from '../i18n/LanguageContext';
import type { ConversationTurn, EmotionTurnSnapshot } from '../types/domain';

const CHART_HEIGHT = 300;
const PADDING = { top: 24, right: 22, bottom: 48, left: 54 } as const;
const VAD_MIN = -1;
const VAD_MAX = 1;
const MIN_AXIS_SPAN = 0.3;
const MIN_AXIS_PADDING = 0.06;
const AXIS_PADDING_RATIO = 0.16;
const NICE_TICK_STEPS = [0.05, 0.1, 0.2, 0.25, 0.5, 1] as const;

interface EmotionTrajectoryPoint {
  id: string;
  round: number;
  managerText: string;
  valence: number;
  arousal: number;
  dominance: number;
  anchorId?: string;
}

interface PositionedPoint extends EmotionTrajectoryPoint {
  exactX: number;
  exactY: number;
  x: number;
  y: number;
}

interface AxisDomain {
  min: number;
  max: number;
  step: number;
  ticks: number[];
}

interface EmotionChartDomain {
  valence: AxisDomain;
  arousal: AxisDomain;
}

interface EmotionTrajectoryChartProps {
  turns?: ConversationTurn[];
}

function isVadValue(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value >= -1 && value <= 1;
}

function emotionSnapshot(turn: ConversationTurn): EmotionTurnSnapshot | null {
  const snapshot = turn.metadata?.emotion_snapshot;
  if (
    !snapshot
    || !isVadValue(snapshot.valence)
    || !isVadValue(snapshot.arousal)
    || !isVadValue(snapshot.dominance)
  ) {
    return null;
  }
  return snapshot;
}

export function buildEmotionTrajectoryPoints(
  turns: ConversationTurn[] = [],
  emptyManagerText = '本轮经理原话为空。',
): EmotionTrajectoryPoint[] {
  const points: EmotionTrajectoryPoint[] = [];
  let managerRound = 0;

  turns.forEach((turn, index) => {
    if (turn.speaker !== 'manager') return;
    managerRound += 1;
    const snapshot = emotionSnapshot(turn);
    if (!snapshot) return;

    const managerText = String(turn.text || '').trim() || emptyManagerText;
    const turnIndex = typeof turn.turn_index === 'number' ? turn.turn_index : index + 1;
    points.push({
      id: `${turnIndex}-${managerRound}`,
      round: managerRound,
      managerText,
      valence: snapshot.valence,
      arousal: snapshot.arousal,
      dominance: snapshot.dominance,
      anchorId: snapshot.anchor_id || undefined,
    });
  });

  return points;
}

function rounded(value: number): number {
  return Number(value.toFixed(4));
}

function clampVad(value: number): number {
  return Math.min(VAD_MAX, Math.max(VAD_MIN, value));
}

function niceTickStep(span: number): number {
  const target = span / 4;
  return NICE_TICK_STEPS.find((step) => step >= target) || 1;
}

export function buildAxisDomain(values: number[]): AxisDomain {
  if (!values.length) {
    return { min: VAD_MIN, max: VAD_MAX, step: 0.5, ticks: [-1, -0.5, 0, 0.5, 1] };
  }

  const dataMin = Math.min(...values);
  const dataMax = Math.max(...values);
  const dataSpan = dataMax - dataMin;
  const padding = Math.max(MIN_AXIS_PADDING, dataSpan * AXIS_PADDING_RATIO);
  let min = dataMin - padding;
  let max = dataMax + padding;

  if (max - min < MIN_AXIS_SPAN) {
    const center = (dataMin + dataMax) / 2;
    min = center - MIN_AXIS_SPAN / 2;
    max = center + MIN_AXIS_SPAN / 2;
  }

  if (min < VAD_MIN) {
    max += VAD_MIN - min;
    min = VAD_MIN;
  }
  if (max > VAD_MAX) {
    min -= max - VAD_MAX;
    max = VAD_MAX;
  }
  min = clampVad(min);
  max = clampVad(max);

  const step = niceTickStep(max - min);
  const domainMin = clampVad(Math.floor((min + Number.EPSILON) / step) * step);
  const domainMax = clampVad(Math.ceil((max - Number.EPSILON) / step) * step);
  const tickCount = Math.round((domainMax - domainMin) / step);
  const ticks = Array.from(
    { length: tickCount + 1 },
    (_, index) => rounded(domainMin + index * step),
  );

  return {
    min: rounded(domainMin),
    max: rounded(domainMax),
    step,
    ticks,
  };
}

function buildEmotionChartDomain(points: EmotionTrajectoryPoint[]): EmotionChartDomain {
  return {
    valence: buildAxisDomain(points.map((point) => point.valence)),
    arousal: buildAxisDomain(points.map((point) => point.arousal)),
  };
}

function scaleToPlot(value: number, domain: AxisDomain, length: number): number {
  return ((value - domain.min) / (domain.max - domain.min)) * length;
}

function formatAxisTick(value: number, step: number): string {
  const normalized = Math.abs(value) < 0.0001 ? 0 : value;
  return normalized.toFixed(step < 0.1 ? 2 : 1);
}

function positionPoints(
  points: EmotionTrajectoryPoint[],
  width: number,
  domain: EmotionChartDomain,
): PositionedPoint[] {
  const plotWidth = width - PADDING.left - PADDING.right;
  const plotHeight = CHART_HEIGHT - PADDING.top - PADDING.bottom;
  const duplicateIndexes = new Map<string, number>();

  return points.map((point) => {
    const exactX = PADDING.left + scaleToPlot(point.valence, domain.valence, plotWidth);
    const exactY = PADDING.top + plotHeight - scaleToPlot(point.arousal, domain.arousal, plotHeight);
    const duplicateKey = `${point.valence.toFixed(3)}:${point.arousal.toFixed(3)}`;
    const duplicateIndex = duplicateIndexes.get(duplicateKey) || 0;
    duplicateIndexes.set(duplicateKey, duplicateIndex + 1);

    if (duplicateIndex === 0) return { ...point, exactX, exactY, x: exactX, y: exactY };

    const angle = ((duplicateIndex - 1) % 8) * (Math.PI / 4);
    const radius = 7 + Math.floor((duplicateIndex - 1) / 8) * 4;
    return {
      ...point,
      exactX,
      exactY,
      x: exactX + Math.cos(angle) * radius,
      y: exactY + Math.sin(angle) * radius,
    };
  });
}

export default function EmotionTrajectoryChart({ turns = [] }: EmotionTrajectoryChartProps) {
  const { translate, translateTemplate } = useLanguage();
  const canvasRef = useRef<HTMLDivElement>(null);
  const [chartWidth, setChartWidth] = useState(720);
  const [hoveredId, setHoveredId] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const points = buildEmotionTrajectoryPoints(
    turns,
    translate('本轮经理原话为空。', 'The manager did not say anything in this turn.'),
  );
  const hasPoints = points.length > 0;
  const domain = buildEmotionChartDomain(points);
  const positionedPoints = positionPoints(points, chartWidth, domain);
  const activeId = hoveredId || selectedId;
  const activePoint = positionedPoints.find((point) => point.id === activeId);
  const plotWidth = chartWidth - PADDING.left - PADDING.right;
  const plotHeight = CHART_HEIGHT - PADDING.top - PADDING.bottom;

  useLayoutEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return undefined;

    const updateWidth = () => setChartWidth(Math.max(240, Math.round(canvas.clientWidth)));
    updateWidth();
    if (typeof ResizeObserver === 'undefined') {
      window.addEventListener('resize', updateWidth);
      return () => window.removeEventListener('resize', updateWidth);
    }
    const observer = new ResizeObserver(updateWidth);
    observer.observe(canvas);
    return () => observer.disconnect();
  }, [hasPoints]);

  const togglePoint = (pointId: string) => {
    setSelectedId((current) => (current === pointId ? null : pointId));
  };

  const handlePointKeyDown = (event: KeyboardEvent<SVGGElement>, pointId: string) => {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    event.preventDefault();
    togglePoint(pointId);
  };

  return (
    <section className="emotion-trajectory" aria-label={translate('员工情绪变化轨迹', 'Employee emotion trajectory')}>
      <header className="emotion-trajectory-header">
        <div>
          <Activity size={18} aria-hidden="true" />
          <h4>{translate('员工情绪轨迹', 'Employee emotion trajectory')}</h4>
        </div>
        <span>{translate('愉快程度 × 激动程度', 'Pleasure × arousal')}</span>
      </header>

      {!hasPoints ? (
        <p className="emotion-trajectory-empty">{translate('本次会话暂无可用的逐轮情绪数据。', 'No turn-by-turn emotion data is available for this conversation.')}</p>
      ) : (
        <div
          className="emotion-trajectory-canvas"
          ref={canvasRef}
          onClick={() => setSelectedId(null)}
        >
          <svg
            viewBox={`0 0 ${chartWidth} ${CHART_HEIGHT}`}
            role="img"
            aria-label={translateTemplate(
              '员工情绪在 {count} 个经理对话轮次中的愉快程度与激动程度变化轨迹',
              'Employee pleasure and arousal across {count} manager turns',
              { count: points.length },
            )}
          >
            <rect
              className="emotion-trajectory-plot-surface"
              x={PADDING.left}
              y={PADDING.top}
              width={plotWidth}
              height={plotHeight}
              rx={16}
            />
            {domain.valence.ticks.map((tick) => {
              const x = PADDING.left + scaleToPlot(tick, domain.valence, plotWidth);
              return (
                <g key={`x-${tick}`}>
                  <line
                    className={`emotion-trajectory-grid ${Math.abs(tick) < 0.0001 ? 'is-zero' : ''}`}
                    x1={x}
                    y1={PADDING.top}
                    x2={x}
                    y2={PADDING.top + plotHeight}
                  />
                  <text className="emotion-trajectory-tick" x={x} y={CHART_HEIGHT - 27} textAnchor="middle">
                    {formatAxisTick(tick, domain.valence.step)}
                  </text>
                </g>
              );
            })}
            {domain.arousal.ticks.map((tick) => {
              const y = PADDING.top + plotHeight - scaleToPlot(tick, domain.arousal, plotHeight);
              return (
                <g key={`y-${tick}`}>
                  <line
                    className={`emotion-trajectory-grid ${Math.abs(tick) < 0.0001 ? 'is-zero' : ''}`}
                    x1={PADDING.left}
                    y1={y}
                    x2={PADDING.left + plotWidth}
                    y2={y}
                  />
                  <text className="emotion-trajectory-tick" x={PADDING.left - 10} y={y + 4} textAnchor="end">
                    {formatAxisTick(tick, domain.arousal.step)}
                  </text>
                </g>
              );
            })}

            <text
              className="emotion-trajectory-axis-label"
              x={PADDING.left + plotWidth / 2}
              y={CHART_HEIGHT - 5}
              textAnchor="middle"
            >
              {translate('愉快程度（V）', 'Pleasure (V)')}
            </text>
            <text
              className="emotion-trajectory-axis-label"
              x={15}
              y={PADDING.top + plotHeight / 2}
              textAnchor="middle"
              transform={`rotate(-90 15 ${PADDING.top + plotHeight / 2})`}
            >
              {translate('激动程度（A）', 'Arousal (A)')}
            </text>

            {positionedPoints.length > 1 && (
              <polyline
                className="emotion-trajectory-line"
                points={positionedPoints.map((point) => `${point.exactX},${point.exactY}`).join(' ')}
              />
            )}

            {positionedPoints.map((point) => (
              <g
                className={`emotion-trajectory-node ${activeId === point.id ? 'is-active' : ''}`}
                key={point.id}
                role="button"
                tabIndex={0}
                aria-label={translateTemplate(
                  '第 {round} 轮，愉快程度 {valence}，激动程度 {arousal}，主导程度 {dominance}。经理说：{manager_text}',
                  'Turn {round}: pleasure {valence}, arousal {arousal}, dominance {dominance}. Manager: {manager_text}',
                  {
                    round: point.round,
                    valence: point.valence.toFixed(2),
                    arousal: point.arousal.toFixed(2),
                    dominance: point.dominance.toFixed(2),
                    manager_text: point.managerText,
                  },
                )}
                onPointerEnter={() => setHoveredId(point.id)}
                onPointerLeave={() => setHoveredId(null)}
                onFocus={() => setHoveredId(point.id)}
                onBlur={() => setHoveredId(null)}
                onClick={(event) => {
                  event.stopPropagation();
                  togglePoint(point.id);
                }}
                onKeyDown={(event) => handlePointKeyDown(event, point.id)}
              >
                <circle className="emotion-trajectory-node-hit" cx={point.x} cy={point.y} r={20} />
                <circle className="emotion-trajectory-node-halo" cx={point.x} cy={point.y} r={14} />
                <circle className="emotion-trajectory-node-dot" cx={point.x} cy={point.y} r={9} />
                <text className="emotion-trajectory-node-number" x={point.x} y={point.y + 3.25} textAnchor="middle">
                  {point.round}
                </text>
              </g>
            ))}
          </svg>

          {activePoint && (
            <div
              className={`emotion-trajectory-tooltip ${activePoint.x < chartWidth / 3 ? 'is-left' : activePoint.x > chartWidth * 0.67 ? 'is-right' : ''} ${activePoint.y < 112 ? 'is-below' : ''}`}
              style={{
                left: `${(activePoint.x / chartWidth) * 100}%`,
                top: `${(activePoint.y / CHART_HEIGHT) * 100}%`,
              } as CSSProperties}
              role="status"
              onClick={(event) => event.stopPropagation()}
            >
              <div className="emotion-trajectory-tooltip-head">
                <strong>{translateTemplate('第 {round} 轮', 'Turn {round}', { round: activePoint.round })}</strong>
              </div>
              <div className="emotion-trajectory-metrics" aria-label={translate('情绪程度数值', 'Emotion values')}>
                <span><b>{translate('愉快程度', 'Pleasure')}</b><em>{activePoint.valence.toFixed(2)}</em></span>
                <span><b>{translate('激动程度', 'Arousal')}</b><em>{activePoint.arousal.toFixed(2)}</em></span>
                <span><b>{translate('主导程度', 'Dominance')}</b><em>{activePoint.dominance.toFixed(2)}</em></span>
              </div>
              <p><b>{translate('经理：', 'Manager: ')}</b>{activePoint.managerText}</p>
            </div>
          )}
        </div>
      )}
    </section>
  );
}

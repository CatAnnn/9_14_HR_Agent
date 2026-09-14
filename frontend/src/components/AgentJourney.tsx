import {
  type ComponentType,
  type ReactNode,
} from 'react';
import {
  ArrowRight,
  BarChart3,
  BookOpen,
  CheckCircle2,
  MessageSquare,
  Mic,
  SlidersHorizontal,
  Sparkles,
  Target,
  TrendingUp,
  UserRound,
  type LucideIcon,
} from 'lucide-react';
import { Link } from 'react-router-dom';
import {
  getAgentIntroductionJourneySteps,
  type AgentIntroductionContent,
  type AgentJourneyPreviewContentMap,
  type AgentJourneyStepId,
  type LocalizedIntroductionText,
} from '../content/agent-introduction-content';
import { useAgentJourneyProgress } from '../hooks/useAgentJourneyProgress';

type Localize = (value: LocalizedIntroductionText) => string;

const JOURNEY_ROUTE_WIDTH = 1232;
const JOURNEY_ROUTE_STEP_HEIGHT = 808;

function buildDesktopRoutePath(stepCount: number): string {
  const parts = ['M1085 0'];
  let startX = 1085;

  for (let index = 0; index < stepCount; index += 1) {
    const top = index * JOURNEY_ROUTE_STEP_HEIGHT;
    const endX = startX === 1085 ? 147 : 1085;
    const direction = endX > startX ? 1 : -1;
    parts.push(
      `V${top + 48}`,
      `Q${startX} ${top + 72} ${startX + direction * 24} ${top + 72}`,
      `H${endX - direction * 24}`,
      `Q${endX} ${top + 72} ${endX} ${top + 96}`,
      `V${top + JOURNEY_ROUTE_STEP_HEIGHT}`,
    );
    startX = endX;
  }

  return parts.join(' ');
}

function buildMobileRoutePath(stepCount: number): string {
  const parts = ['M160 0'];

  for (let index = 0; index < stepCount; index += 1) {
    const top = index * JOURNEY_ROUTE_STEP_HEIGHT;
    parts.push(
      `V${top + 48}`,
      `Q160 ${top + 72} 136 ${top + 72}`,
      `H42 Q18 ${top + 72} 18 ${top + 96}`,
      `V${top + 144} Q18 ${top + 168} 42 ${top + 168}`,
      `H278 Q302 ${top + 168} 302 ${top + 192}`,
      `V${top + 240} Q302 ${top + 264} 278 ${top + 264}`,
      `H184 Q160 ${top + 264} 160 ${top + 288}`,
      `V${top + JOURNEY_ROUTE_STEP_HEIGHT}`,
    );
  }

  return parts.join(' ');
}

interface AgentJourneyPreviewProps {
  readonly previews: AgentJourneyPreviewContentMap;
  readonly localize: Localize;
}

interface PreviewFrameProps {
  readonly title: string;
  readonly className: string;
  readonly children: ReactNode;
}

function PreviewFrame({
  title,
  className,
  children,
}: PreviewFrameProps) {
  return (
    <div className={`agent-journey-preview-frame ${className}`}>
      <div className="agent-journey-preview-heading">
        <span className="agent-journey-preview-mark" />
        <strong>{title}</strong>
      </div>
      <div className="agent-journey-preview-body">{children}</div>
    </div>
  );
}

function ProfilePreview({ previews, localize }: AgentJourneyPreviewProps) {
  const preview = previews.profile;
  return (
    <PreviewFrame title={localize(preview.heading)} className="is-profile">
      <div className="agent-preview-profile-heading">
        <span className="agent-preview-avatar">LF</span>
        <div>
          <strong>{localize(preview.metadata[0])}</strong>
          <span>{localize(preview.metadata[1])}</span>
        </div>
      </div>
      <div className="agent-preview-profile-meta">
        {preview.metadata.slice(2).map((item) => (
          <span key={item.zh}>{localize(item)}</span>
        ))}
      </div>
      <div className="agent-preview-goals">
        <strong>{localize(preview.goalsHeading)}</strong>
        {preview.goals.map((goal) => (
          <span key={goal.zh}><CheckCircle2 size={13} />{localize(goal)}</span>
        ))}
      </div>
    </PreviewFrame>
  );
}

function IntentPreview({ previews, localize }: AgentJourneyPreviewProps) {
  const preview = previews.intent;
  return (
    <PreviewFrame title={localize(preview.heading)} className="is-intent">
      <div className="agent-preview-intents">
        {preview.options.map((option, index) => (
          <span key={option.zh} className={index === 0 ? 'is-selected' : undefined}>
            {localize(option)}
          </span>
        ))}
      </div>
    </PreviewFrame>
  );
}

function IntentPerformancePreview({ previews, localize }: AgentJourneyPreviewProps) {
  const preview = previews.intentPerformance;
  return (
    <PreviewFrame
      title={localize(preview.heading)}
      className="is-intent-performance"
    >
      <div className="agent-preview-comparison">
        <div>
          <strong>{localize(preview.goalHeading)}</strong>
          <p>{localize(preview.goal)}</p>
        </div>
        <ArrowRight size={18} aria-hidden="true" />
        <div>
          <strong>{localize(preview.performanceHeading)}</strong>
          <p>{localize(preview.performance)}</p>
        </div>
      </div>
    </PreviewFrame>
  );
}

function SimulationPreview({ previews, localize }: AgentJourneyPreviewProps) {
  const preview = previews.simulation;
  const traitValues = [72, 84, 66];
  return (
    <PreviewFrame title={localize(preview.heading)} className="is-simulation">
      <div className="agent-preview-traits">
        {preview.traits.map((trait, index) => (
          <div key={trait.zh}>
            <span>{localize(trait)}</span>
            <i><b style={{ width: `${traitValues[index]}%` }} /></i>
          </div>
        ))}
      </div>
      <div className="agent-preview-needs">
        <strong>{localize(preview.primaryNeed)}</strong>
        <div>
          {preview.supportingNeeds.map((need) => (
            <span key={need.zh}>{localize(need)}</span>
          ))}
        </div>
      </div>
    </PreviewFrame>
  );
}

function GuidancePreview({ previews, localize }: AgentJourneyPreviewProps) {
  const preview = previews.guidance;
  return (
    <PreviewFrame title={localize(preview.heading)} className="is-guidance">
      <div className="agent-preview-guidance-list">
        {preview.dimensions.map((dimension, index) => (
          <div key={dimension.zh} className={index === 0 ? 'is-open' : undefined}>
            <span>{String(index + 1).padStart(2, '0')}</span>
            <strong>{localize(dimension)}</strong>
            {index === 0 && <p>{localize(preview.detail)}</p>}
          </div>
        ))}
      </div>
    </PreviewFrame>
  );
}

function RehearsalPreview({ previews, localize }: AgentJourneyPreviewProps) {
  const preview = previews.rehearsal;
  return (
    <PreviewFrame title={localize(preview.heading)} className="is-rehearsal">
      <div className="agent-preview-dialogue">
        <p className="is-manager"><span>M</span>{localize(preview.manager)}</p>
        <p className="is-employee"><span>E</span>{localize(preview.employee)}</p>
      </div>
      <div className="agent-preview-rehearsal-footer">
        <span><Mic size={14} />{localize(preview.voice)}</span>
        <span>{localize(preview.emotion)}</span>
      </div>
    </PreviewFrame>
  );
}

function ReportPreview({ previews, localize }: AgentJourneyPreviewProps) {
  const preview = previews.report;
  return (
    <PreviewFrame title={localize(preview.heading)} className="is-report">
      <div className="agent-preview-report-grid">
        {preview.dimensions.map((dimension, index) => (
          <span key={dimension.zh} className={index === 2 ? 'is-watch' : undefined}>
            {localize(dimension)}
          </span>
        ))}
      </div>
      <div className="agent-preview-trajectory">
        <strong>{localize(preview.trajectory)}</strong>
        <svg viewBox="0 0 260 64" preserveAspectRatio="none" aria-hidden="true">
          <path d="M4 48 C42 38 52 45 82 31 S137 38 164 20 S215 30 256 10" />
          <circle cx="4" cy="48" r="3" />
          <circle cx="82" cy="31" r="3" />
          <circle cx="164" cy="20" r="3" />
          <circle cx="256" cy="10" r="3" />
        </svg>
      </div>
      <p className="agent-preview-improvement">{localize(preview.improvement)}</p>
    </PreviewFrame>
  );
}

const previewRegistry = {
  profile: ProfilePreview,
  intent: IntentPreview,
  intentPerformance: IntentPerformancePreview,
  simulation: SimulationPreview,
  guidance: GuidancePreview,
  rehearsal: RehearsalPreview,
  report: ReportPreview,
} satisfies Record<keyof AgentJourneyPreviewContentMap, ComponentType<AgentJourneyPreviewProps>>;

const stepIcons = {
  profile: UserRound,
  intent: Target,
  'intent-performance': TrendingUp,
  simulation: SlidersHorizontal,
  guidance: BookOpen,
  rehearsal: MessageSquare,
  report: BarChart3,
} satisfies Record<AgentJourneyStepId, LucideIcon>;

export interface AgentJourneyProps {
  readonly content: AgentIntroductionContent;
  readonly localize: Localize;
  readonly starting: boolean;
  readonly onStart: () => void;
  readonly onPrefetch: () => void;
}

export function AgentJourney({
  content,
  localize,
  starting,
  onStart,
  onPrefetch,
}: AgentJourneyProps) {
  const journeySteps = getAgentIntroductionJourneySteps(content);
  const {
    containerRef,
    setLaunchRef,
    setLaunchPathRef,
    setStepRef,
    setRoutePathRef,
    activeIndex,
    reachedIndex,
  } = useAgentJourneyProgress(journeySteps.length);
  const routeHeight = journeySteps.length * JOURNEY_ROUTE_STEP_HEIGHT;
  const desktopRoutePath = buildDesktopRoutePath(journeySteps.length);
  const mobileRoutePath = buildMobileRoutePath(journeySteps.length);

  return (
    <section className="agent-journey" aria-labelledby="agent-journey-title">
      <div ref={setLaunchRef} className="agent-journey-launch">
        <svg
          className="agent-journey-launch-route"
          viewBox="0 0 1232 414"
          preserveAspectRatio="none"
          aria-hidden="true"
        >
          <path
            className="is-base"
            d="M386 35 H1061 Q1085 35 1085 59 V414"
          />
          <path
            ref={setLaunchPathRef}
            className="is-progress"
            d="M386 35 H1061 Q1085 35 1085 59 V414"
            pathLength="100"
            strokeDasharray="100 100"
            strokeDashoffset="100"
          />
        </svg>

        <div className="agent-journey-start">
          <h2 id="agent-journey-title">{localize(content.journey.startTitle)}</h2>
          <p>{localize(content.journey.startDescription)}</p>
        </div>

        <div className="agent-journey-activation">
          <Sparkles size={17} aria-hidden="true" />
          <strong>{localize(content.stepsTitle)}</strong>
        </div>
      </div>

      <ol ref={containerRef} className="agent-journey-list">
        <svg
          className="agent-journey-route-map is-desktop"
          viewBox={`0 0 ${JOURNEY_ROUTE_WIDTH} ${routeHeight}`}
          preserveAspectRatio="none"
          aria-hidden="true"
        >
          <path className="is-base" d={desktopRoutePath} pathLength="1" />
          <path
            ref={(node) => setRoutePathRef(0, node)}
            className="is-progress"
            d={desktopRoutePath}
            pathLength="100"
            strokeDasharray="100 100"
          />
        </svg>
        <svg
          className="agent-journey-route-map is-mobile"
          viewBox={`0 0 320 ${routeHeight}`}
          preserveAspectRatio="none"
          aria-hidden="true"
        >
          <path className="is-base" d={mobileRoutePath} pathLength="1" />
          <path
            ref={(node) => setRoutePathRef(1, node)}
            className="is-progress"
            d={mobileRoutePath}
            pathLength="100"
            strokeDasharray="100 100"
          />
        </svg>

        {journeySteps.map((step, index) => {
          const Preview = previewRegistry[step.previewId];
          const StepIcon = stepIcons[step.id];
          const active = index === activeIndex;
          const reached = index <= reachedIndex;

          return (
            <li
              key={step.id}
              ref={(node) => setStepRef(index, node)}
              className={[
                'agent-journey-step',
                index % 2 === 0 ? 'is-copy-left' : 'is-copy-right',
                active ? 'is-active' : '',
                reached ? 'is-reached' : '',
              ].filter(Boolean).join(' ')}
              aria-current={active ? 'step' : undefined}
            >
              <span className="agent-journey-route-label" aria-hidden="true">
                {step.number}
              </span>

              <div className="agent-journey-stage">
                <div className="agent-journey-copy">
                  <h3 className="agent-journey-tag">
                    <StepIcon size={17} aria-hidden="true" />
                    <span>{localize(step.title)}</span>
                  </h3>
                  <div className="agent-journey-explanation">
                    <p>{localize(step.guide.task)}</p>
                    <p>{localize(step.guide.meaning)}</p>
                  </div>
                </div>

                <div className="agent-journey-preview" aria-hidden="true">
                  <Preview previews={content.journey.previews} localize={localize} />
                </div>
              </div>
            </li>
          );
        })}
      </ol>

      <footer className="agent-journey-end">
        <span aria-hidden="true"><CheckCircle2 size={26} /></span>
        <h2>{localize(content.journey.endTitle)}</h2>
        <p>{localize(content.journey.endDescription)}</p>
        <div className="agent-journey-actions">
          <Link to="/">{localize(content.backLabel)}</Link>
          <button
            type="button"
            onClick={onStart}
            onFocus={onPrefetch}
            onPointerEnter={onPrefetch}
            disabled={starting}
          >
            <span>{localize(starting ? content.startingLabel : content.startLabel)}</span>
            {!starting && <ArrowRight size={18} aria-hidden="true" />}
          </button>
        </div>
      </footer>
    </section>
  );
}

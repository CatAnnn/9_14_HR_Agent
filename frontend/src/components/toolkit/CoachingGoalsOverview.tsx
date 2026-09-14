import {
  AlertTriangle,
  ArrowRight,
  BrainCircuit,
  Check,
  RefreshCw,
  Target,
  UsersRound,
} from 'lucide-react';
import { Link } from 'react-router-dom';
import { coachingGoalsOverviewContent as content } from '../../content/toolkit-articles/coaching-goals-overview';
import '../../styles/toolkit-coaching-goals.css';

export function CoachingGoalsOverview() {
  return (
    <article className="coaching-goals-overview" aria-labelledby="coaching-goals-overview-title">
      <section className="cgo-editorial">
        <div className="cgo-shell">
          <div className="cgo-editorial-grid">
            <div className="cgo-editorial-copy">
              <p className="cgo-eyebrow">{content.introduction.eyebrow}</p>
              <h2 id="coaching-goals-overview-title">{content.introduction.title}</h2>
              <p className="cgo-lead">{content.introduction.lead}</p>
              <div className="cgo-editorial-body">
                {content.introduction.paragraphs.map((paragraph) => (
                  <p key={paragraph}>{paragraph}</p>
                ))}
              </div>
            </div>

            <aside className="cgo-origin" aria-label="GROW 方法脉络">
              <span>{content.introduction.origin.label}</span>
              <h3>{content.introduction.origin.title}</h3>
              <p>{content.introduction.origin.description}</p>
              <strong>{content.introduction.origin.note}</strong>
            </aside>
          </div>

          <div className="cgo-learning-loop" aria-label="绩效与学习循环">
            <div>
              <span>{content.introduction.learningLoop.label}</span>
              <h3>{content.introduction.learningLoop.title}</h3>
              <p>{content.introduction.learningLoop.description}</p>
            </div>
            <ol>
              {content.introduction.learningLoop.steps.map((step, index) => (
                <li key={step}>
                  <span>{String(index + 1).padStart(2, '0')}</span>
                  <strong>{step}</strong>
                </li>
              ))}
            </ol>
          </div>
        </div>
      </section>

      <section className="cgo-principles" aria-labelledby="cgo-principles-title">
        <div className="cgo-shell">
          <header className="cgo-principles-heading">
            <div>
              <p className="cgo-eyebrow">{content.principles.eyebrow}</p>
              <h2 id="cgo-principles-title">{content.principles.title}</h2>
            </div>
            <p>{content.principles.introduction}</p>
          </header>

          <div className="cgo-principles-grid">
            {content.principles.items.map((principle, index) => {
              const Icon = index === 0 ? BrainCircuit : Check;
              return (
                <article key={principle.english}>
                  <div className="cgo-principle-meta">
                    <span>{principle.number}</span>
                    <Icon aria-hidden="true" />
                    <strong>{principle.english}</strong>
                  </div>
                  <h3>{principle.title}</h3>
                  <p>{principle.description}</p>
                  <small>{principle.signal}</small>
                </article>
              );
            })}
          </div>

          <p className="cgo-principles-bridge">{content.principles.bridge}</p>
        </div>
      </section>

      <section className="cgo-grow-loop" aria-labelledby="cgo-grow-title">
        <div className="cgo-shell">
          <header className="cgo-section-heading">
            <div>
              <p className="cgo-eyebrow">{content.growLoop.eyebrow}</p>
              <h2 id="cgo-grow-title">{content.growLoop.title}</h2>
            </div>
            <p>{content.growLoop.introduction}</p>
          </header>

          <ol className="cgo-grow-stages">
            {content.growLoop.stages.map((stage) => (
              <li key={stage.letter}>
                <div className="cgo-stage-mark" aria-hidden="true">
                  <span>{stage.letter}</span>
                  <small>{stage.english}</small>
                </div>
                <h3>{stage.title}</h3>
                <p>{stage.description}</p>
                <blockquote>{stage.question}</blockquote>
                <strong>{stage.output}</strong>
              </li>
            ))}
          </ol>

          <div className="cgo-loop-note">
            <RefreshCw aria-hidden="true" />
            <p>{content.growLoop.loopNote}</p>
          </div>
        </div>
      </section>

      <section className="cgo-manager" aria-labelledby="cgo-manager-title">
        <div className="cgo-shell">
          <header className="cgo-section-heading">
            <div>
              <p className="cgo-eyebrow">{content.managerShift.eyebrow}</p>
              <h2 id="cgo-manager-title">{content.managerShift.title}</h2>
            </div>
            <p>{content.managerShift.introduction}</p>
          </header>

          <table className="cgo-manager-table">
            <thead>
              <tr>
                <th scope="col">{content.managerShift.columns.dimension}</th>
                <th scope="col">{content.managerShift.columns.directive}</th>
                <th scope="col">{content.managerShift.columns.coaching}</th>
              </tr>
            </thead>
            <tbody>
              {content.managerShift.rows.map((row) => (
                <tr key={row.dimension}>
                  <th scope="row">{row.dimension}</th>
                  <td data-label={content.managerShift.columns.directive}>{row.directive}</td>
                  <td data-label={content.managerShift.columns.coaching}>{row.coaching}</td>
                </tr>
              ))}
            </tbody>
          </table>

          <div className="cgo-listening">
            <div>
              <UsersRound aria-hidden="true" />
              <h3>{content.managerShift.listening.title}</h3>
              <p>{content.managerShift.listening.description}</p>
            </div>
            <ul>
              {content.managerShift.listening.prompts.map((prompt) => (
                <li key={prompt.label}>
                  <span>{prompt.label}</span>
                  <p>{prompt.question}</p>
                </li>
              ))}
            </ul>
          </div>
        </div>
      </section>

      <section className="cgo-boundaries" aria-labelledby="cgo-boundaries-title">
        <div className="cgo-shell cgo-boundaries-grid">
          <div className="cgo-boundaries-heading">
            <AlertTriangle aria-hidden="true" />
            <p className="cgo-eyebrow">{content.boundaries.eyebrow}</p>
            <h2 id="cgo-boundaries-title">{content.boundaries.title}</h2>
            <p>{content.boundaries.introduction}</p>
          </div>

          <div>
            <ol className="cgo-boundary-list">
              {content.boundaries.items.map((item, index) => (
                <li key={item.title}>
                  <span>{String(index + 1).padStart(2, '0')}</span>
                  <div>
                    <h3>{item.title}</h3>
                    <p>{item.description}</p>
                  </div>
                </li>
              ))}
            </ol>
            <p className="cgo-boundary-note">{content.boundaries.note}</p>
          </div>
        </div>
      </section>

      <section className="cgo-culture" aria-labelledby="cgo-culture-title">
        <div className="cgo-shell">
          <header className="cgo-section-heading">
            <div>
              <p className="cgo-eyebrow">{content.culture.eyebrow}</p>
              <h2 id="cgo-culture-title">{content.culture.title}</h2>
            </div>
            <p>{content.culture.introduction}</p>
          </header>

          <ol className="cgo-culture-path">
            {content.culture.steps.map((step) => (
              <li key={step.number}>
                <span>{step.number}</span>
                <div>
                  <h3>{step.title}</h3>
                  <p>{step.description}</p>
                  <small>{step.evidence}</small>
                </div>
              </li>
            ))}
          </ol>
        </div>
      </section>

      <section className="cgo-tool-guide" aria-labelledby="cgo-tool-guide-title">
        <div className="cgo-shell">
          <header className="cgo-section-heading">
            <div>
              <p className="cgo-eyebrow">{content.toolGuide.eyebrow}</p>
              <h2 id="cgo-tool-guide-title">{content.toolGuide.title}</h2>
            </div>
            <p>{content.toolGuide.introduction}</p>
          </header>

          <ol className="cgo-tool-guide-list">
            {content.toolGuide.items.map((tool) => (
              <li key={tool.href}>
                <Link to={tool.href} aria-label={`${tool.linkLabel}：${tool.question}`}>
                  <span className="cgo-tool-number">{tool.number}</span>
                  <div className="cgo-tool-name">
                    <Target aria-hidden="true" />
                    <h3>{tool.title}</h3>
                  </div>
                  <div className="cgo-tool-context">
                    <strong>{tool.question}</strong>
                    <p>{tool.description}</p>
                  </div>
                  <span className="cgo-tool-link-label">
                    {tool.linkLabel}
                    <ArrowRight aria-hidden="true" />
                  </span>
                </Link>
              </li>
            ))}
          </ol>
        </div>
      </section>
    </article>
  );
}

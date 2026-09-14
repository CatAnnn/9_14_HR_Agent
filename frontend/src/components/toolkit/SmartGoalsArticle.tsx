import {
  AlertTriangle,
  ArrowRight,
  Check,
  ClipboardCheck,
  Lightbulb,
  RefreshCw,
  Target,
  TrendingUp,
} from 'lucide-react';
import { smartGoalsArticle } from '../../content/toolkit-articles/smart-goals';
import '../../styles/toolkit-smart-goals.css';

export function SmartGoalsArticle() {
  const {
    overview,
    criteria,
    example,
    writing,
    workConnection,
    tracking,
    comparison,
    tradeoffs,
    mistakes,
    checklist,
  } = smartGoalsArticle;

  return (
    <article className="smart-goals-article" aria-label="SMART Goals 实践指南正文">
      <section id="smart-definition" className="smart-goals-overview" aria-labelledby="smart-definition-title">
        <div className="smart-goals-inner smart-goals-overview-grid">
          <div className="smart-goals-prose">
            <span className="smart-goals-kicker"><Target aria-hidden="true" /> Definition and value</span>
            <h2 id="smart-definition-title">{overview.title}</h2>
            <p className="smart-goals-lead">{overview.lead}</p>
            {overview.paragraphs.map((paragraph) => <p key={paragraph}>{paragraph}</p>)}
          </div>
          <aside className="smart-goals-value-rail" aria-label="SMART 目标的三项价值">
            {overview.valuePoints.map((item, index) => (
              <div key={item.title}>
                <span>{String(index + 1).padStart(2, '0')}</span>
                <h3>{item.title}</h3>
                <p>{item.detail}</p>
              </div>
            ))}
          </aside>
        </div>
      </section>

      <section id="smart-criteria" className="smart-goals-criteria" aria-labelledby="smart-criteria-title">
        <div className="smart-goals-inner">
          <header className="smart-goals-section-heading">
            <span className="smart-goals-kicker"><Lightbulb aria-hidden="true" /> The five criteria</span>
            <h2 id="smart-criteria-title">{criteria.title}</h2>
            <p>{criteria.description}</p>
          </header>
          <ol className="smart-goals-criteria-list">
            {criteria.items.map((item, index) => (
              <li key={item.letter}>
                <span className="smart-goals-criterion-letter" aria-hidden="true">{item.letter}</span>
                <header>
                  <small>{String(index + 1).padStart(2, '0')} · {item.focus}</small>
                  <h3><span>{item.term}</span>{item.label}</h3>
                </header>
                <div className="smart-goals-criterion-detail">
                  <p>{item.definition}</p>
                  <ul>
                    {item.questions.map((question) => <li key={question}>{question}</li>)}
                  </ul>
                </div>
                <blockquote>
                  <strong>示例推进</strong>
                  <p>{item.example}</p>
                </blockquote>
              </li>
            ))}
          </ol>
          <p className="smart-goals-distinction"><strong>A / R 边界</strong>{criteria.distinction}</p>
        </div>
      </section>

      <section id="smart-example" className="smart-goals-example" aria-labelledby="smart-example-title">
        <div className="smart-goals-inner">
          <header className="smart-goals-section-heading">
            <span className="smart-goals-kicker"><TrendingUp aria-hidden="true" /> Before and after</span>
            <h2 id="smart-example-title">{example.title}</h2>
            <p>{example.description}</p>
          </header>

          <div className="smart-goals-example-contrast">
            <article className="is-before">
              <span>{example.before.label}</span>
              <h3>{example.before.statement}</h3>
              <p>{example.before.diagnosis}</p>
            </article>
            <ArrowRight aria-hidden="true" />
            <article className="is-after">
              <span>{example.after.label}</span>
              <h3>{example.after.statement}</h3>
              <p>{example.after.context}</p>
            </article>
          </div>

          <ol className="smart-goals-example-stages" aria-label="目标逐步改写过程">
            {example.stages.map((stage, index) => (
              <li key={stage.criterion}>
                <span>{String(index + 1).padStart(2, '0')}</span>
                <div>
                  <h3>{stage.criterion}</h3>
                  <p>{stage.change}</p>
                </div>
                <small><Check aria-hidden="true" />{stage.evidence}</small>
              </li>
            ))}
          </ol>
        </div>
      </section>

      <section id="smart-writing" className="smart-goals-writing" aria-labelledby="smart-writing-title">
        <div className="smart-goals-inner">
          <header className="smart-goals-section-heading">
            <span className="smart-goals-kicker"><ClipboardCheck aria-hidden="true" /> Writing process</span>
            <h2 id="smart-writing-title">{writing.title}</h2>
            <p>{writing.description}</p>
          </header>

          <ol className="smart-goals-writing-steps">
            {writing.steps.map((step, index) => (
              <li key={step.title}>
                <span>{String(index + 1).padStart(2, '0')}</span>
                <div><h3>{step.title}</h3><p>{step.detail}</p></div>
              </li>
            ))}
          </ol>

          <div className="smart-goals-builder" role="group" aria-labelledby="smart-goals-builder-title">
            <header>
              <span>Goal builder</span>
              <h3 id="smart-goals-builder-title">{writing.builder.title}</h3>
              <p>{writing.builder.description}</p>
            </header>
            <dl>
              {writing.builder.fields.map((field, index) => (
                <div key={field.label}>
                  <span aria-hidden="true">{String(index + 1).padStart(2, '0')}</span>
                  <dt>{field.label}</dt>
                  <dd>{field.prompt}</dd>
                </div>
              ))}
            </dl>
            <div className="smart-goals-template">
              <div>
                <strong>核心目标陈述</strong>
                <p>{writing.builder.goalTemplate}</p>
              </div>
              <div>
                <strong>执行上下文</strong>
                <p>{writing.builder.contextTemplate}</p>
              </div>
            </div>
          </div>
        </div>
      </section>

      <section id="smart-work" className="smart-goals-work" aria-labelledby="smart-work-title">
        <div className="smart-goals-inner">
          <header className="smart-goals-section-heading">
            <span className="smart-goals-kicker"><Target aria-hidden="true" /> Goal to work</span>
            <h2 id="smart-work-title">{workConnection.title}</h2>
            <p>{workConnection.description}</p>
          </header>
          <ol className="smart-goals-work-chain">
            {workConnection.layers.map((layer) => (
              <li key={layer.level}>
                <span>{layer.level}</span>
                <h3>{layer.title}</h3>
                <p>{layer.detail}</p>
                <small>{layer.question}</small>
              </li>
            ))}
          </ol>
          <ul className="smart-goals-operating-rules">
            {workConnection.rules.map((rule) => (
              <li key={rule}><Check aria-hidden="true" /><span>{rule}</span></li>
            ))}
          </ul>
        </div>
      </section>

      <section id="smart-tracking" className="smart-goals-tracking" aria-labelledby="smart-tracking-title">
        <div className="smart-goals-inner">
          <header className="smart-goals-section-heading">
            <span className="smart-goals-kicker"><RefreshCw aria-hidden="true" /> Tracking and review</span>
            <h2 id="smart-tracking-title">{tracking.title}</h2>
            <p>{tracking.description}</p>
          </header>

          <div className="smart-goals-table-wrap">
            <table className="smart-goals-scoreboard">
              <caption>{tracking.asOf}</caption>
              <thead>
                <tr><th>结果指标</th><th>基线</th><th>目标</th><th>当前</th><th>状态</th><th>下一步行动</th></tr>
              </thead>
              <tbody>
                {tracking.rows.map((row) => (
                  <tr key={row.metric}>
                    <th scope="row">{row.metric}</th>
                    <td data-label="基线">{row.baseline}</td>
                    <td data-label="目标">{row.target}</td>
                    <td data-label="当前">{row.current}</td>
                    <td data-label="状态"><span data-status={row.status}>{row.status}</span></td>
                    <td data-label="下一步行动">{row.next}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="smart-goals-review-grid">
            <ol className="smart-goals-cadence-list">
              {tracking.cadences.map((item, index) => (
                <li key={item.cadence}>
                  <span>{String(index + 1).padStart(2, '0')}</span>
                  <div><small>{item.cadence}</small><h3>{item.title}</h3><p>{item.detail}</p></div>
                </li>
              ))}
            </ol>
            <aside className="smart-goals-review-questions" aria-labelledby="smart-review-questions-title">
              <h3 id="smart-review-questions-title">每次复盘都要回答</h3>
              <ol>
                {tracking.reviewQuestions.map((question, index) => (
                  <li key={question}><span>{String(index + 1).padStart(2, '0')}</span>{question}</li>
                ))}
              </ol>
            </aside>
          </div>
        </div>
      </section>

      <section id="smart-okr" className="smart-goals-comparison" aria-labelledby="smart-okr-title">
        <div className="smart-goals-inner">
          <header className="smart-goals-section-heading">
            <span className="smart-goals-kicker"><RefreshCw aria-hidden="true" /> Framework boundary</span>
            <h2 id="smart-okr-title">{comparison.title}</h2>
            <p>{comparison.description}</p>
          </header>
          <div className="smart-goals-table-wrap">
            <table className="smart-goals-framework-table">
              <thead><tr><th>比较维度</th><th>SMART</th><th>OKR</th></tr></thead>
              <tbody>
                {comparison.rows.map((row) => (
                  <tr key={row.dimension}>
                    <th scope="row">{row.dimension}</th>
                    <td data-label="SMART">{row.smart}</td>
                    <td data-label="OKR">{row.okr}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <blockquote className="smart-goals-boundary-note">
            <strong>{comparison.boundary.title}</strong>
            <p>{comparison.boundary.detail}</p>
          </blockquote>
        </div>
      </section>

      <section id="smart-tradeoffs" className="smart-goals-tradeoffs" aria-labelledby="smart-tradeoffs-title">
        <div className="smart-goals-inner">
          <header className="smart-goals-section-heading">
            <span className="smart-goals-kicker"><Lightbulb aria-hidden="true" /> Strengths and limits</span>
            <h2 id="smart-tradeoffs-title">{tradeoffs.title}</h2>
            <p>{tradeoffs.description}</p>
          </header>
          <div className="smart-goals-tradeoff-columns">
            <div>
              <h3><Check aria-hidden="true" />主要优点</h3>
              <ol>
                {tradeoffs.benefits.map((item, index) => (
                  <li key={item.title}><span>{String(index + 1).padStart(2, '0')}</span><div><h4>{item.title}</h4><p>{item.detail}</p></div></li>
                ))}
              </ol>
            </div>
            <div>
              <h3><AlertTriangle aria-hidden="true" />限制与应对</h3>
              <ol>
                {tradeoffs.limitations.map((item, index) => (
                  <li key={item.title}><span>{String(index + 1).padStart(2, '0')}</span><div><h4>{item.title}</h4><p>{item.detail}</p></div></li>
                ))}
              </ol>
            </div>
          </div>
        </div>
      </section>

      <section id="smart-mistakes" className="smart-goals-mistakes" aria-labelledby="smart-mistakes-title">
        <div className="smart-goals-inner">
          <header className="smart-goals-section-heading">
            <span className="smart-goals-kicker"><AlertTriangle aria-hidden="true" /> Common mistakes</span>
            <h2 id="smart-mistakes-title">{mistakes.title}</h2>
            <p>{mistakes.description}</p>
          </header>
          <ol className="smart-goals-mistake-list">
            {mistakes.items.map((item, index) => (
              <li key={item.problem}>
                <span>{String(index + 1).padStart(2, '0')}</span>
                <div><h3>{item.problem}</h3><p>{item.fix}</p></div>
              </li>
            ))}
          </ol>
        </div>
      </section>

      <section id="smart-checklist" className="smart-goals-checklist" aria-labelledby="smart-checklist-title">
        <div className="smart-goals-inner smart-goals-checklist-grid">
          <header className="smart-goals-section-heading">
            <span className="smart-goals-kicker"><ClipboardCheck aria-hidden="true" /> Final review</span>
            <h2 id="smart-checklist-title">{checklist.title}</h2>
            <p>{checklist.description}</p>
            <blockquote><p>{checklist.takeaway}</p></blockquote>
          </header>
          <ul aria-label="SMART 目标发布检查清单">
            {checklist.items.map((item, index) => (
              <li key={item}>
                <label>
                  <input type="checkbox" />
                  <span><small>{String(index + 1).padStart(2, '0')}</small>{item}</span>
                </label>
              </li>
            ))}
          </ul>
        </div>
      </section>
    </article>
  );
}

export default SmartGoalsArticle;

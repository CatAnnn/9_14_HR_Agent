import {
  AlertTriangle,
  ArrowRight,
  Check,
  ClipboardCheck,
  Lightbulb,
  MessageSquare,
  RefreshCw,
  Search,
  Target,
  UsersRound,
  type LucideIcon,
} from 'lucide-react';
import { growModelArticle } from '../../content/toolkit-articles/grow-model';
import '../../styles/toolkit-grow-model.css';

const stageIcons = {
  goal: Target,
  reality: Search,
  options: Lightbulb,
  will: ClipboardCheck,
} satisfies Record<(typeof growModelArticle.stages)[number]['id'], LucideIcon>;

export function GrowModelArticle() {
  const {
    overview,
    stages,
    loopback,
    dialogue,
    session,
    applications,
    selfCoaching,
    guardrails,
    actionPlan,
  } = growModelArticle;

  return (
    <article className="grow-model-article" aria-label="GROW Model 实践指南">
      <section id="grow-overview" className="grow-section grow-overview" aria-labelledby="grow-overview-title">
        <div className="grow-shell grow-overview-layout">
          <header className="grow-section-heading">
            <span className="grow-kicker"><UsersRound aria-hidden="true" />Origin &amp; purpose</span>
            <h2 id="grow-overview-title">{overview.title}</h2>
          </header>
          <div className="grow-overview-copy">
            {overview.paragraphs.map((paragraph) => <p key={paragraph}>{paragraph}</p>)}
            <blockquote>
              <strong>{overview.principleTitle}</strong>
              <p>{overview.principle}</p>
            </blockquote>
            <ul className="grow-working-agreement" aria-label="开始对话前的三项约定">
              {overview.workingAgreement.map((item) => (
                <li key={item}><Check aria-hidden="true" /><span>{item}</span></li>
              ))}
            </ul>
          </div>
        </div>
      </section>

      <section id="grow-framework" className="grow-section grow-framework" aria-labelledby="grow-framework-title">
        <div className="grow-shell">
          <header className="grow-section-heading grow-section-heading-wide">
            <span className="grow-kicker"><Target aria-hidden="true" />Conversation path</span>
            <h2 id="grow-framework-title">从想要的结果，走到愿意承担的下一步</h2>
            <p>四个阶段各自产生一种关键成果。点击阶段可直接进入对应问题库。</p>
          </header>

          <ol className="grow-stage-path" aria-label="GROW 四阶段路径">
            {stages.map((stage, index) => {
              const Icon = stageIcons[stage.id];
              return (
                <li className={`is-${stage.id}`} key={stage.id}>
                  <a href={`#grow-question-${stage.id}`}>
                    <span className="grow-stage-mark"><Icon aria-hidden="true" />{stage.letter}</span>
                    <small>{stage.english}</small>
                    <h3>{stage.chinese}</h3>
                    <p>{stage.headline}</p>
                  </a>
                  {index < stages.length - 1 && <ArrowRight className="grow-stage-arrow" aria-hidden="true" />}
                </li>
              );
            })}
          </ol>

          <div className="grow-stage-details">
            {stages.map((stage) => (
              <section className={`is-${stage.id}`} aria-labelledby={`grow-stage-${stage.id}-title`} key={stage.id}>
                <div className="grow-stage-detail-label">
                  <span>{stage.letter}</span>
                  <h3 id={`grow-stage-${stage.id}-title`}>{stage.english} · {stage.chinese}</h3>
                </div>
                <p>{stage.purpose}</p>
                <strong>{stage.output}</strong>
              </section>
            ))}
          </div>

          <aside className="grow-loopback" aria-labelledby="grow-loopback-title">
            <header>
              <RefreshCw aria-hidden="true" />
              <div>
                <h3 id="grow-loopback-title">{loopback.title}</h3>
                <p>{loopback.description}</p>
              </div>
            </header>
            <div className="grow-loopback-routes">
              {loopback.routes.map((route) => (
                <div key={`${route.from}-${route.to}`}>
                  <strong>{route.from}<ArrowRight aria-hidden="true" />{route.to}</strong>
                  <p>{route.when}</p>
                </div>
              ))}
            </div>
          </aside>
        </div>
      </section>

      <section id="grow-questions" className="grow-section grow-questions" aria-labelledby="grow-questions-title">
        <div className="grow-shell">
          <header className="grow-section-heading grow-section-heading-wide">
            <span className="grow-kicker"><Search aria-hidden="true" />Question bank</span>
            <h2 id="grow-questions-title">问题的作用是打开思考，不是引向预设答案</h2>
            <p>不必逐题照问。根据对方刚刚说出的信息，选择最能增加清晰度的一到两个问题，并留出思考与沉默的空间。</p>
          </header>

          <div className="grow-question-bank">
            {stages.map((stage) => {
              const Icon = stageIcons[stage.id];
              return (
                <section
                  id={`grow-question-${stage.id}`}
                  className={`grow-question-stage is-${stage.id}`}
                  aria-labelledby={`grow-question-${stage.id}-title`}
                  key={stage.id}
                >
                  <header>
                    <span><Icon aria-hidden="true" />{stage.letter}</span>
                    <div>
                      <small>{stage.english}</small>
                      <h3 id={`grow-question-${stage.id}-title`}>{stage.chinese}阶段</h3>
                    </div>
                  </header>
                  <ol>
                    {stage.questions.map((question, index) => (
                      <li key={question}>
                        <span>{String(index + 1).padStart(2, '0')}</span>
                        <p>{question}</p>
                      </li>
                    ))}
                  </ol>
                </section>
              );
            })}
          </div>
        </div>
      </section>

      <section id="grow-dialogue" className="grow-section grow-dialogue" aria-labelledby="grow-dialogue-title">
        <div className="grow-shell">
          <header className="grow-section-heading grow-section-heading-wide">
            <span className="grow-kicker"><MessageSquare aria-hidden="true" />Worked dialogue</span>
            <h2 id="grow-dialogue-title">{dialogue.title}</h2>
            <p>{dialogue.description}</p>
          </header>

          <div className="grow-dialogue-layout">
            <ol className="grow-conversation" aria-label="GROW 教练对话示例">
              {dialogue.turns.map((turn, index) => (
                <li className={`is-${turn.stageId}`} key={`${turn.stage}-${index}`}>
                  <span className="grow-turn-stage">{turn.stage}</span>
                  <div>
                    <header>
                      <strong>{turn.speaker}</strong>
                      {'loopback' in turn && turn.loopback && (
                        <span className="grow-turn-loopback"><RefreshCw aria-hidden="true" />{turn.loopback}</span>
                      )}
                    </header>
                    <p>{turn.text}</p>
                  </div>
                </li>
              ))}
            </ol>

            <aside className="grow-dialogue-takeaways" aria-labelledby="grow-dialogue-takeaways-title">
              <span>Conversation readout</span>
              <h3 id="grow-dialogue-takeaways-title">这段对话推进了什么</h3>
              <ol>
                {dialogue.takeaways.map((item, index) => (
                  <li key={item}><span>{String(index + 1).padStart(2, '0')}</span><p>{item}</p></li>
                ))}
              </ol>
            </aside>
          </div>
        </div>
      </section>

      <section id="grow-session" className="grow-section grow-session" aria-labelledby="grow-session-title">
        <div className="grow-shell">
          <header className="grow-section-heading grow-section-heading-wide">
            <span className="grow-kicker"><RefreshCw aria-hidden="true" />Session flow</span>
            <h2 id="grow-session-title">{session.title}</h2>
            <p>{session.description}</p>
          </header>

          <ol className="grow-session-flow">
            {session.steps.map((step, index) => (
              <li key={step.title}>
                <time>{step.time}</time>
                <span className="grow-session-index">{String(index + 1).padStart(2, '0')}</span>
                <div>
                  <h3>{step.title}</h3>
                  <p>{step.detail}</p>
                </div>
                <strong><span>阶段产出</span>{step.output}</strong>
              </li>
            ))}
          </ol>
        </div>
      </section>

      <section id="grow-practice" className="grow-section grow-practice" aria-labelledby="grow-practice-title">
        <div className="grow-shell">
          <header className="grow-section-heading grow-section-heading-wide">
            <span className="grow-kicker"><UsersRound aria-hidden="true" />Where to use</span>
            <h2 id="grow-practice-title">在有选择空间的议题上，GROW 最能发挥作用</h2>
          </header>

          <div className="grow-practice-layout">
            <section aria-labelledby="grow-applications-title">
              <span className="grow-subsection-label">Applied coaching</span>
              <h3 id="grow-applications-title">{applications.title}</h3>
              <ul className="grow-application-list">
                {applications.items.map((item) => (
                  <li key={item.title}>
                    <Check aria-hidden="true" />
                    <div><strong>{item.title}</strong><p>{item.detail}</p></div>
                  </li>
                ))}
              </ul>
            </section>

            <section aria-labelledby="grow-self-coaching-title">
              <span className="grow-subsection-label">Self coaching</span>
              <h3 id="grow-self-coaching-title">{selfCoaching.title}</h3>
              <p className="grow-self-intro">{selfCoaching.description}</p>
              <ol className="grow-self-steps">
                {selfCoaching.steps.map((step) => (
                  <li key={step.label}>
                    <span>{step.label}</span>
                    <div><strong>{step.title}</strong><p>{step.detail}</p></div>
                  </li>
                ))}
              </ol>
            </section>
          </div>
        </div>
      </section>

      <section id="grow-guardrails" className="grow-section grow-guardrails" aria-labelledby="grow-guardrails-title">
        <div className="grow-shell">
          <header className="grow-section-heading grow-section-heading-wide">
            <span className="grow-kicker"><AlertTriangle aria-hidden="true" />Limits &amp; pitfalls</span>
            <h2 id="grow-guardrails-title">{guardrails.title}</h2>
          </header>

          <div className="grow-guardrail-layout">
            <section aria-labelledby="grow-limitations-title">
              <h3 id="grow-limitations-title">使用边界</h3>
              <ul>
                {guardrails.limitations.map((item) => (
                  <li key={item.title}>
                    <AlertTriangle aria-hidden="true" />
                    <div><strong>{item.title}</strong><p>{item.detail}</p></div>
                  </li>
                ))}
              </ul>
            </section>

            <section aria-labelledby="grow-mistakes-title">
              <h3 id="grow-mistakes-title">常见错误与修正</h3>
              <ol>
                {guardrails.mistakes.map((item, index) => (
                  <li key={item.title}>
                    <span>{String(index + 1).padStart(2, '0')}</span>
                    <div>
                      <strong>{item.title}</strong>
                      <p><b>修正：</b>{item.correction}</p>
                    </div>
                  </li>
                ))}
              </ol>
            </section>
          </div>
        </div>
      </section>

      <section id="grow-action" className="grow-section grow-action" aria-labelledby="grow-action-title">
        <div className="grow-shell">
          <header className="grow-section-heading grow-section-heading-wide">
            <span className="grow-kicker"><ClipboardCheck aria-hidden="true" />Commit &amp; review</span>
            <h2 id="grow-action-title">{actionPlan.title}</h2>
            <p>{actionPlan.description}</p>
          </header>

          <div className="grow-action-goal">
            <span>目标</span>
            <p>{actionPlan.goal}</p>
          </div>

          <div className="grow-action-table-wrap">
            <table className="grow-action-table">
              <caption>GROW 对话行动计划示例</caption>
              <thead>
                <tr>
                  <th>行动</th>
                  <th>时间</th>
                  <th>完成证据</th>
                  <th>障碍预案</th>
                  <th>责任与复盘</th>
                </tr>
              </thead>
              <tbody>
                {actionPlan.rows.map((row) => (
                  <tr key={row.action}>
                    <th scope="row">{row.action}</th>
                    <td data-label="时间">{row.timing}</td>
                    <td data-label="完成证据">{row.evidence}</td>
                    <td data-label="障碍预案">{row.contingency}</td>
                    <td data-label="责任与复盘">{row.accountability}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="grow-action-footer">
            <section className="grow-commitment" aria-labelledby="grow-commitment-title">
              <span>{actionPlan.commitment.label}</span>
              <div>
                <strong id="grow-commitment-title">{actionPlan.commitment.score}</strong>
                <div className="grow-commitment-meter" role="img" aria-label="当前承诺度 8 分，共 10 分">
                  {Array.from({ length: 10 }, (_, index) => (
                    <i className={index < 8 ? 'is-active' : ''} key={index} />
                  ))}
                </div>
              </div>
              <p>{actionPlan.commitment.note}</p>
            </section>

            <section className="grow-review" aria-labelledby="grow-review-title">
              <span>Next reality</span>
              <h3 id="grow-review-title">复盘时，把结果带回 Reality</h3>
              <ol>
                {actionPlan.reviewQuestions.map((question, index) => (
                  <li key={question}><span>{String(index + 1).padStart(2, '0')}</span><p>{question}</p></li>
                ))}
              </ol>
            </section>
          </div>
        </div>
      </section>
    </article>
  );
}

import {
  ArrowUpRight,
  BarChart3,
  CalendarClock,
  CheckCircle2,
  ClipboardList,
  MessageSquareText,
  ShieldCheck,
  Target,
  UsersRound,
} from 'lucide-react';
import type { ReactNode } from 'react';
import { threeSixtyFeedbackContent as content } from '../../content/toolkit-articles/360-degree-feedback';
import '../../styles/toolkit-360-feedback.css';

export function ThreeSixtyFeedbackArticle() {
  return (
    <article className="f360-article" aria-labelledby="f360-title">
      <header className="f360-intro" id="f360-overview">
        <div className="f360-shell f360-intro-grid">
          <div>
            <p className="f360-kicker">{content.intro.eyebrow}</p>
            <h2 id="f360-title">{content.intro.title}</h2>
            <p className="f360-lead">{content.intro.lead}</p>
          </div>
          <ul className="f360-principles" aria-label="360 度反馈实施原则">
            {content.intro.principles.map((principle) => (
              <li key={principle}>
                <CheckCircle2 aria-hidden="true" />
                <span>{principle}</span>
              </li>
            ))}
          </ul>
        </div>
      </header>

      <section id="f360-coverage" className="f360-section" aria-labelledby="f360-coverage-title">
        <div className="f360-shell">
          <SectionHeading
            icon={<UsersRound aria-hidden="true" />}
            eyebrow="Coverage"
            title="评价者覆盖：看见同一行为的不同影响"
            id="f360-coverage-title"
            description="评价者不是越多越好，而是要持续观察过目标行为，并覆盖不同协作关系。"
          />
          <div className="f360-rater-grid">
            {content.raterGroups.map((group) => (
              <article className="f360-rater" key={group.id}>
                <div className="f360-rater-head">
                  <h3>{group.label}</h3>
                  <span>{group.target}</span>
                </div>
                <p>{group.perspective}</p>
                <small>{group.reportingRule}</small>
              </article>
            ))}
          </div>
        </div>
      </section>

      <section id="f360-blueprint" className="f360-section f360-section-muted" aria-labelledby="f360-blueprint-title">
        <div className="f360-shell">
          <SectionHeading
            icon={<ClipboardList aria-hidden="true" />}
            eyebrow="Question blueprint"
            title="题项蓝图：测行为，不测人格印象"
            id="f360-blueprint-title"
            description="每个题项只描述一个动作，让评价者判断自己实际观察到的频率。"
          />

          <div className="f360-scale" aria-labelledby="f360-scale-title">
            <div>
              <h3 id="f360-scale-title">{content.scale.title}</h3>
              <p>{content.scale.description}</p>
            </div>
            <ol>
              {content.scale.points.map((point) => (
                <li key={point.value}>
                  <strong>{point.value}</strong>
                  <span>{point.label}</span>
                </li>
              ))}
            </ol>
          </div>

          <div className="f360-question-list">
            {content.questionBlueprint.map((item, index) => (
              <details key={item.competency} open={index === 0}>
                <summary>
                  <span>{String(index + 1).padStart(2, '0')}</span>
                  <strong>{item.competency}</strong>
                </summary>
                <div className="f360-question-detail">
                  <p><b>行为题项</b>{item.behavior}</p>
                  <p><b>避免写法</b>{item.avoid}</p>
                  <p><b>开放追问</b>{item.followUp}</p>
                </div>
              </details>
            ))}
          </div>
        </div>
      </section>

      <section id="f360-process" className="f360-section" aria-labelledby="f360-process-title">
        <div className="f360-shell">
          <SectionHeading
            icon={<CalendarClock aria-hidden="true" />}
            eyebrow="6-step cycle"
            title="六步实施：从题项到发展计划"
            id="f360-process-title"
            description="完整周期通常需要数周。稳定的规则、节奏和会谈质量比追求快速发报告更重要。"
          />
          <ol className="f360-process">
            {content.process.map((step) => (
              <li key={step.number}>
                <div className="f360-step-number">{step.number}</div>
                <div className="f360-step-body">
                  <div>
                    <h3>{step.title}</h3>
                    <p>{step.purpose}</p>
                  </div>
                  <ul>
                    {step.actions.map((action) => <li key={action}>{action}</li>)}
                  </ul>
                  <span>交付物：{step.output}</span>
                </div>
              </li>
            ))}
          </ol>
        </div>
      </section>

      <section id="f360-compare" className="f360-section f360-section-dark" aria-labelledby="f360-compare-title">
        <div className="f360-shell">
          <SectionHeading
            icon={<BarChart3 aria-hidden="true" />}
            eyebrow="Read the pattern"
            title="自评与他评差异：用于提问，不用于定罪"
            id="f360-compare-title"
            description="示例数据只演示阅读方法。真实报告应同时查看样本量、各组分布和评论情境。"
          />
          <div className="f360-table-wrap">
            <table className="f360-table">
              <caption className="f360-sr-only">自评与他评差异示例</caption>
              <thead>
                <tr>
                  <th scope="col">可观察行为</th>
                  <th scope="col">自评</th>
                  <th scope="col">他评均值</th>
                  <th scope="col">差异</th>
                  <th scope="col">阅读方式</th>
                </tr>
              </thead>
              <tbody>
                {content.comparison.map((row) => (
                  <tr key={row.behavior}>
                    <th scope="row" data-label="可观察行为">{row.behavior}</th>
                    <td data-label="自评">{row.self}</td>
                    <td data-label="他评均值">{row.others}</td>
                    <td data-label="差异"><strong>{row.gap}</strong></td>
                    <td data-label="阅读方式">{row.reading}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="f360-reading-grid">
            {content.reportGuide.map((item, index) => (
              <article key={item.title}>
                <small>{String(index + 1).padStart(2, '0')}</small>
                <h3>{item.title}</h3>
                <p>{item.detail}</p>
              </article>
            ))}
          </div>
        </div>
      </section>

      <section id="f360-debrief" className="f360-section" aria-labelledby="f360-debrief-title">
        <div className="f360-shell">
          <SectionHeading
            icon={<MessageSquareText aria-hidden="true" />}
            eyebrow="Debrief"
            title="反馈会谈：先理解，再选择行动"
            id="f360-debrief-title"
            description="会谈者要承接情绪、澄清模式和情境，避免逐条辩护或猜测匿名评价者。"
          />
          <ol className="f360-debrief">
            {content.debrief.map((item, index) => (
              <li key={item.phase}>
                <span>{String(index + 1).padStart(2, '0')}</span>
                <div><h3>{item.phase}</h3><p>{item.prompt}</p></div>
              </li>
            ))}
          </ol>
        </div>
      </section>

      <section id="f360-action" className="f360-section f360-section-muted" aria-labelledby="f360-action-title">
        <div className="f360-shell">
          <SectionHeading
            icon={<Target aria-hidden="true" />}
            eyebrow="Action"
            title="从一项行为开始，持续 60–90 天"
            id="f360-action-title"
            description="把发展重点放进真实工作场景，用他人可以观察的证据跟踪变化。"
          />
          <div className="f360-table-wrap f360-table-wrap-light">
            <table className="f360-table f360-action-table">
              <caption className="f360-sr-only">360 度反馈行动与跟进计划</caption>
              <thead>
                <tr>
                  <th scope="col">时间</th>
                  <th scope="col">行动</th>
                  <th scope="col">行为证据</th>
                  <th scope="col">支持方式</th>
                </tr>
              </thead>
              <tbody>
                {content.actionPlan.map((row) => (
                  <tr key={row.horizon}>
                    <th scope="row" data-label="时间">{row.horizon}</th>
                    <td data-label="行动">{row.action}</td>
                    <td data-label="行为证据">{row.evidence}</td>
                    <td data-label="支持方式">{row.support}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </section>

      <section id="f360-safeguards" className="f360-section" aria-labelledby="f360-safeguards-title">
        <div className="f360-shell">
          <SectionHeading
            icon={<ShieldCheck aria-hidden="true" />}
            eyebrow="Safeguards"
            title="可信项目的六项治理护栏"
            id="f360-safeguards-title"
            description="保密承诺必须通过产品规则、报告阈值和管理行为共同兑现。"
          />
          <div className="f360-safeguards">
            {content.safeguards.map((item) => (
              <article key={item.title}>
                <ShieldCheck aria-hidden="true" />
                <div><h3>{item.title}</h3><p>{item.detail}</p></div>
              </article>
            ))}
          </div>

          <aside className="f360-source" aria-label="内容来源">
            <div>
              <strong>方法来源</strong>
              <p>{content.source.note}</p>
              <small>访问日期：{content.source.accessedAt}</small>
            </div>
            <a href={content.source.url} target="_blank" rel="noreferrer">
              {content.source.title}
              <ArrowUpRight aria-hidden="true" />
              <span className="f360-sr-only">（在新窗口打开）</span>
            </a>
          </aside>
        </div>
      </section>
    </article>
  );
}

function SectionHeading({
  icon,
  eyebrow,
  title,
  id,
  description,
}: {
  icon: ReactNode;
  eyebrow: string;
  title: string;
  id: string;
  description: string;
}) {
  return (
    <header className="f360-section-heading">
      <p>{icon}<span>{eyebrow}</span></p>
      <h2 id={id}>{title}</h2>
      <span>{description}</span>
    </header>
  );
}

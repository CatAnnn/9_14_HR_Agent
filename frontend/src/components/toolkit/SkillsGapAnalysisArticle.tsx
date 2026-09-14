import {
  AlertTriangle,
  Check,
  Database,
  ExternalLink,
  Layers3,
  Route,
  Target,
} from 'lucide-react';
import { skillsGapAnalysisArticle } from '../../content/toolkit-articles/skills-gap-analysis';
import '../../styles/toolkit-skills-gap.css';

export function SkillsGapAnalysisArticle() {
  const article = skillsGapAnalysisArticle;

  return (
    <div className="skills-gap-article">
      <section id="skills-gap-overview" className="sga-section sga-overview" aria-labelledby="skills-gap-overview-title">
        <div className="sga-shell">
          <header className="sga-lead-header">
            <p className="sga-kicker"><Target aria-hidden="true" />{article.overview.kicker}</p>
            <h2 id="skills-gap-overview-title">{article.overview.title}</h2>
            <p className="sga-lead">{article.overview.lead}</p>
          </header>
          <div className="sga-prose-columns">
            {article.overview.paragraphs.map((paragraph) => <p key={paragraph}>{paragraph}</p>)}
          </div>
          <div className="sga-value-list" aria-label="技能差距分析的业务价值">
            {article.overview.valueItems.map((item, index) => (
              <article key={item.title}>
                <span className="sga-index">{String(index + 1).padStart(2, '0')}</span>
                <div>
                  <small>{item.label}</small>
                  <h3>{item.title}</h3>
                  <p>{item.detail}</p>
                </div>
              </article>
            ))}
          </div>
        </div>
      </section>

      <section id="skills-gap-types" className="sga-section sga-gap-types" aria-labelledby="skills-gap-types-title">
        <div className="sga-shell">
          <header className="sga-section-header">
            <p className="sga-kicker"><Layers3 aria-hidden="true" />差距分类</p>
            <h2 id="skills-gap-types-title">{article.gapTypes.title}</h2>
            <p>{article.gapTypes.description}</p>
          </header>
          <div className="sga-type-grid">
            {article.gapTypes.items.map((item) => (
              <article key={item.code}>
                <span className="sga-index">{item.code}</span>
                <div>
                  <h3>{item.title}</h3>
                  <strong>{item.signal}</strong>
                  <p>{item.detail}</p>
                </div>
              </article>
            ))}
          </div>
          <p className="sga-editorial-note"><strong>双重视角</strong>{article.gapTypes.note}</p>
        </div>
      </section>

      <section id="skills-gap-levels" className="sga-section sga-levels" aria-labelledby="skills-gap-levels-title">
        <div className="sga-shell">
          <header className="sga-section-header sga-section-header-wide">
            <p className="sga-kicker">分析尺度</p>
            <h2 id="skills-gap-levels-title">{article.levels.title}</h2>
            <p>{article.levels.description}</p>
          </header>
          <div className="sga-responsive-table">
            <table className="sga-level-table">
              <thead>
                <tr>
                  <th scope="col">分析层级</th>
                  <th scope="col">典型触发情境</th>
                  <th scope="col">核心问题</th>
                  <th scope="col">应形成的产出</th>
                </tr>
              </thead>
              <tbody>
                {article.levels.items.map((item) => (
                  <tr key={item.level}>
                    <th scope="row" data-label="分析层级">{item.level}</th>
                    <td data-label="典型触发情境">{item.trigger}</td>
                    <td data-label="核心问题">{item.question}</td>
                    <td data-label="应形成的产出">{item.output}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </section>

      <section id="skills-gap-process" className="sga-section sga-process" aria-labelledby="skills-gap-process-title">
        <div className="sga-shell">
          <header className="sga-section-header">
            <p className="sga-kicker"><Route aria-hidden="true" />分析流程</p>
            <h2 id="skills-gap-process-title">{article.process.title}</h2>
            <p>{article.process.description}</p>
          </header>
          <div className="sga-phase-list">
            {article.process.phases.map((phase) => (
              <article className="sga-phase" key={phase.phase}>
                <header>
                  <span>{phase.phase}</span>
                  <h3>{phase.title}</h3>
                  <p>{phase.summary}</p>
                </header>
                <ol>
                  {phase.steps.map((step) => (
                    <li key={step.number}>
                      <span className="sga-step-number">{step.number}</span>
                      <div>
                        <h4>{step.title}</h4>
                        <p>{step.detail}</p>
                        <small><strong>本步产出</strong>{step.deliverable}</small>
                      </div>
                    </li>
                  ))}
                </ol>
              </article>
            ))}
          </div>

          <div className="sga-scale-block" aria-labelledby="skills-gap-scale-title">
            <header>
              <p>统一量规</p>
              <h3 id="skills-gap-scale-title">五级能力行为锚点</h3>
              <span>等级描述应针对具体能力补充代表性任务，以下结构用于统一评分语言。</span>
            </header>
            <div className="sga-scale" role="list">
              {article.process.scale.map((item) => (
                <article key={item.level} role="listitem">
                  <b>{item.level}</b>
                  <h4>{item.name}</h4>
                  <p>{item.anchor}</p>
                </article>
              ))}
            </div>
          </div>
        </div>
      </section>

      <section id="skills-gap-matrix" className="sga-section sga-matrix-section" aria-labelledby="skills-gap-matrix-title">
        <div className="sga-shell sga-shell-wide">
          <header className="sga-section-header sga-section-header-wide">
            <p className="sga-kicker">能力矩阵</p>
            <h2 id="skills-gap-matrix-title">{article.matrix.title}</h2>
            <p>{article.matrix.description}</p>
          </header>

          <div className="sga-status-legend" aria-label="优先级状态图例">
            {article.matrix.legend.map((item) => (
              <div key={item.label}>
                <span className={`sga-status-dot is-${item.tone}`} aria-hidden="true" />
                <p><strong>{item.label}</strong>{item.detail}</p>
              </div>
            ))}
          </div>

          <div className="sga-responsive-table sga-matrix-wrap">
            <table className="sga-matrix-table">
              <thead>
                <tr>
                  <th scope="col">能力</th>
                  <th scope="col">关键工作场景</th>
                  <th scope="col">现状</th>
                  <th scope="col">目标</th>
                  <th scope="col">差距</th>
                  <th scope="col">影响</th>
                  <th scope="col">频率</th>
                  <th scope="col">证据</th>
                  <th scope="col">状态</th>
                </tr>
              </thead>
              <tbody>
                {article.matrix.rows.map((row) => (
                  <tr key={row.skill}>
                    <th scope="row" data-label="能力">{row.skill}</th>
                    <td data-label="关键工作场景">{row.scenario}</td>
                    <td data-label="现状"><span className="sga-score">{row.current}</span></td>
                    <td data-label="目标"><span className="sga-score is-target">{row.target}</span></td>
                    <td data-label="差距"><strong className="sga-gap-score">{row.gap}</strong></td>
                    <td data-label="业务影响">{row.impact}/5</td>
                    <td data-label="使用频率">{row.frequency}/5</td>
                    <td data-label="证据">{row.evidence}</td>
                    <td data-label="状态"><span className={`sga-status is-${row.tone}`}>{row.priority}</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="sga-formula">
            <strong>{article.matrix.formula}</strong>
            <p>{article.matrix.formulaNote}</p>
          </div>
        </div>
      </section>

      <section id="skills-gap-evidence" className="sga-section sga-evidence" aria-labelledby="skills-gap-evidence-title">
        <div className="sga-shell">
          <header className="sga-section-header">
            <p className="sga-kicker"><Database aria-hidden="true" />证据体系</p>
            <h2 id="skills-gap-evidence-title">{article.evidence.title}</h2>
            <p>{article.evidence.description}</p>
          </header>
          <div className="sga-evidence-groups">
            {article.evidence.groups.map((group) => (
              <section key={group.title}>
                <h3>{group.title}</h3>
                <div>
                  {group.items.map((item) => (
                    <article key={item.source}>
                      <h4>{item.source}</h4>
                      <p>{item.use}</p>
                      <small><strong>校验提醒</strong>{item.caution}</small>
                    </article>
                  ))}
                </div>
              </section>
            ))}
          </div>
          <div className="sga-principles" aria-label="数据使用原则">
            {article.evidence.principles.map((item, index) => (
              <article key={item.title}>
                <span>{String(index + 1).padStart(2, '0')}</span>
                <h3>{item.title}</h3>
                <p>{item.detail}</p>
              </article>
            ))}
          </div>
        </div>
      </section>

      <section id="skills-gap-priority" className="sga-section sga-priority" aria-labelledby="skills-gap-priority-title">
        <div className="sga-shell">
          <header className="sga-section-header">
            <p className="sga-kicker">决策排序</p>
            <h2 id="skills-gap-priority-title">{article.priority.title}</h2>
            <p>{article.priority.description}</p>
          </header>
          <div className="sga-priority-layout">
            <ol className="sga-criteria-list">
              {article.priority.criteria.map((item) => (
                <li key={item.number}>
                  <span>{item.number}</span>
                  <div><h3>{item.title}</h3><p>{item.detail}</p></div>
                </li>
              ))}
            </ol>
            <aside className="sga-decision-bands" aria-label="优先级决策分层">
              <h3>形成四类明确决策</h3>
              {article.priority.decisions.map((item) => (
                <div key={item.label}>
                  <span className={`sga-status-dot is-${item.tone}`} aria-hidden="true" />
                  <p><strong>{item.label}</strong>{item.detail}</p>
                </div>
              ))}
            </aside>
          </div>
        </div>
      </section>

      <section id="skills-gap-actions" className="sga-section sga-actions" aria-labelledby="skills-gap-actions-title">
        <div className="sga-shell sga-shell-wide">
          <header className="sga-section-header sga-section-header-wide">
            <p className="sga-kicker">干预设计</p>
            <h2 id="skills-gap-actions-title">{article.actions.title}</h2>
            <p>{article.actions.description}</p>
          </header>
          <div className="sga-action-options" role="list" aria-label="技能差距干预方式">
            <div className="sga-action-options-head" aria-hidden="true">
              <span>方式</span><span>适用情形</span><span>可选行动</span>
            </div>
            {article.actions.options.map((item) => (
              <article key={item.label} role="listitem">
                <h3>{item.label}</h3>
                <p data-label="适用情形">{item.useWhen}</p>
                <p data-label="可选行动">{item.actions}</p>
              </article>
            ))}
          </div>
          <div className="sga-plan-template">
            <header>
              <p>行动方案最小字段</p>
              <h3>让每个优先差距都有负责人、路径和验证方式</h3>
            </header>
            <div>
              {article.actions.planFields.map((item, index) => (
                <article key={item.field}>
                  <span>{String(index + 1).padStart(2, '0')}</span>
                  <h4>{item.field}</h4>
                  <p>{item.question}</p>
                </article>
              ))}
            </div>
          </div>
        </div>
      </section>

      <section id="skills-gap-example" className="sga-section sga-example" aria-labelledby="skills-gap-example-title">
        <div className="sga-shell">
          <header className="sga-section-header sga-section-header-wide">
            <p className="sga-kicker">综合应用</p>
            <h2 id="skills-gap-example-title">{article.example.title}</h2>
            <p>{article.example.description}</p>
          </header>
          <dl className="sga-example-facts">
            {article.example.facts.map((item) => (
              <div key={item.label}><dt>{item.label}</dt><dd>{item.value}</dd></div>
            ))}
          </dl>
          <ol className="sga-example-timeline">
            {article.example.timeline.map((item) => (
              <li key={item.period}>
                <span>{item.period}</span>
                <div><h3>{item.title}</h3><p>{item.detail}</p></div>
              </li>
            ))}
          </ol>
          <div className="sga-measures">
            <h3>用四组信号验证行动是否有效</h3>
            <div>
              {article.example.measures.map((item) => (
                <article key={item.type}><h4>{item.type}</h4><p>{item.detail}</p></article>
              ))}
            </div>
          </div>
          <blockquote><strong>示例结论</strong><p>{article.example.takeaway}</p></blockquote>
        </div>
      </section>

      <section id="skills-gap-pitfalls" className="sga-section sga-pitfalls" aria-labelledby="skills-gap-pitfalls-title">
        <div className="sga-shell">
          <header className="sga-section-header">
            <p className="sga-kicker"><AlertTriangle aria-hidden="true" />质量校验</p>
            <h2 id="skills-gap-pitfalls-title">{article.pitfalls.title}</h2>
          </header>
          <div className="sga-pitfall-list">
            {article.pitfalls.items.map((item, index) => (
              <article key={item.mistake}>
                <span>{String(index + 1).padStart(2, '0')}</span>
                <div><h3>{item.mistake}</h3><p>{item.correction}</p></div>
              </article>
            ))}
          </div>
        </div>
      </section>

      <section id="skills-gap-checklist" className="sga-section sga-checklist" aria-labelledby="skills-gap-checklist-title">
        <div className="sga-shell">
          <header className="sga-section-header">
            <p className="sga-kicker">交付前复核</p>
            <h2 id="skills-gap-checklist-title">{article.checklist.title}</h2>
          </header>
          <div className="sga-checklist-groups">
            {article.checklist.groups.map((group) => (
              <section key={group.title}>
                <h3>{group.title}</h3>
                <ul>
                  {group.items.map((item) => (
                    <li key={item}><Check aria-hidden="true" /><span>{item}</span></li>
                  ))}
                </ul>
              </section>
            ))}
          </div>

          <aside className="sga-source-note" aria-label="内容方法来源">
            <div>
              <span>{article.source.label}</span>
              <a href={article.source.url} target="_blank" rel="noreferrer">
                {article.source.title}<ExternalLink aria-hidden="true" />
              </a>
              <small>{article.source.accessedAt}</small>
            </div>
            <p>{article.source.note}</p>
          </aside>
        </div>
      </section>
    </div>
  );
}

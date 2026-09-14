import {
  AlertTriangle,
  ArrowUpRight,
  ClipboardCheck,
  Compass,
  Eye,
  ShieldCheck,
  Target,
} from 'lucide-react';
import { leadershipSelfAssessmentArticle as article } from '../../content/toolkit-articles/leadership-self-assessment';
import '../../styles/toolkit-leadership-self-assessment.css';

function SectionIntro({
  eyebrow,
  title,
  description,
}: {
  eyebrow: string;
  title: string;
  description: string;
}) {
  return (
    <header className="lsa-section-intro">
      <p>{eyebrow}</p>
      <h2>{title}</h2>
      <span>{description}</span>
    </header>
  );
}

export function LeadershipSelfAssessmentArticle() {
  return (
    <article className="lsa-article">
      <div className="lsa-shell">
        <section id="lsa-foundation" className="lsa-section lsa-foundation" aria-labelledby="lsa-foundation-title">
          <div className="lsa-foundation-copy">
            <p className="lsa-kicker"><Compass aria-hidden="true" /> 从自我认知走向发展证据</p>
            <h2 id="lsa-foundation-title">自评的价值，不在于给自己一个分数</h2>
            <p>
              领导力自评是一种结构化反思：用统一能力模型回看近期行为、工作结果与他人体验，
              形成可讨论的发展假设。它快速、易启动，但也容易受盲点、记忆和个人标准影响。
            </p>
            <blockquote>
              <strong>使用边界</strong>
              <p>将自评作为教练对话和发展计划的起点，不把它当作单独的人才结论。</p>
            </blockquote>
          </div>
          <div className="lsa-principles" aria-label="三项评估原则">
            {article.principles.map((item, index) => (
              <article key={item.title}>
                <small>{String(index + 1).padStart(2, '0')}</small>
                <h3>{item.title}</h3>
                <p>{item.detail}</p>
              </article>
            ))}
          </div>
        </section>

        <section id="lsa-competencies" className="lsa-section" aria-labelledby="lsa-competencies-title">
          <SectionIntro
            eyebrow="Competency model"
            title="七项能力，统一讨论语言"
            description="每项能力都从可观察行为开始，并连接可核验的工作证据与反思问题。"
          />
          <div className="lsa-competency-grid">
            {article.competencies.map((item, index) => (
              <details key={item.name} open={index === 0}>
                <summary>
                  <span>{String(index + 1).padStart(2, '0')}</span>
                  <strong>{item.name}</strong>
                  <small>查看行为与证据</small>
                </summary>
                <div>
                  <p>{item.signal}</p>
                  <dl>
                    <div><dt>证据来源</dt><dd>{item.evidence}</dd></div>
                    <div><dt>反思问题</dt><dd>{item.prompt}</dd></div>
                  </dl>
                </div>
              </details>
            ))}
          </div>
        </section>

        <section id="lsa-scale" className="lsa-section lsa-scale-section" aria-labelledby="lsa-scale-title">
          <SectionIntro
            eyebrow="Behavior anchors"
            title="五级行为量规"
            description="先读完整量规，再评分。等级描述的是在多大复杂度下能够稳定表现，而不是人的固定标签。"
          />
          <div className="lsa-table-wrap">
            <table className="lsa-table">
              <thead>
                <tr><th>等级</th><th>判断</th><th>行为锚点</th><th>最低证据要求</th></tr>
              </thead>
              <tbody>
                {article.scale.map((item) => (
                  <tr key={item.level}>
                    <td data-label="等级"><span className="lsa-level">{item.level}</span></td>
                    <td data-label="判断"><strong>{item.label}</strong></td>
                    <td data-label="行为锚点">{item.anchor}</td>
                    <td data-label="最低证据要求">{item.evidence}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>

        <section id="lsa-evidence" className="lsa-section" aria-labelledby="lsa-evidence-title">
          <SectionIntro
            eyebrow="Evidence ledger"
            title="用证据账本替代印象评分"
            description="记录近期情境、自己的具体行为、产生的结果和证据可信度。没有证据时保留判断。"
          />
          <div className="lsa-table-wrap">
            <table className="lsa-table lsa-evidence-table">
              <thead>
                <tr><th>能力</th><th>情境</th><th>可观察行为</th><th>结果</th><th>可信度</th></tr>
              </thead>
              <tbody>
                {article.evidenceLedger.map((item) => (
                  <tr key={item.competency}>
                    <td data-label="能力"><strong>{item.competency}</strong></td>
                    <td data-label="情境">{item.context}</td>
                    <td data-label="可观察行为">{item.behavior}</td>
                    <td data-label="结果">{item.result}</td>
                    <td data-label="可信度"><span className="lsa-confidence">{item.confidence}</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <aside className="lsa-reflection" aria-labelledby="lsa-reflection-title">
            <div>
              <Eye aria-hidden="true" />
              <h3 id="lsa-reflection-title">开放反思</h3>
            </div>
            <ul>
              {article.reflectionPrompts.map((prompt) => <li key={prompt}>{prompt}</li>)}
            </ul>
          </aside>
        </section>

        <section id="lsa-calibration" className="lsa-section lsa-calibration" aria-labelledby="lsa-calibration-title">
          <SectionIntro
            eyebrow="External calibration"
            title="让可信任的人帮助校准"
            description="评分差异不是谁对谁错，而是进一步查找信息、情境和盲点的入口。"
          />
          <ol className="lsa-calibration-steps">
            {article.calibrationSteps.map((item) => (
              <li key={item.step}>
                <span>{item.step}</span>
                <div><h3>{item.title}</h3><p>{item.detail}</p></div>
              </li>
            ))}
          </ol>
          <div className="lsa-bias-panel">
            <header><ShieldCheck aria-hidden="true" /><div><h3>评分前偏差检查</h3><p>逐项确认，减少熟悉感和个人期待对判断的干扰。</p></div></header>
            <div>
              {article.biasChecks.map((item) => (
                <article key={item.name}>
                  <strong>{item.name}</strong>
                  <p>{item.correction}</p>
                </article>
              ))}
            </div>
          </div>
        </section>

        <section id="lsa-profile" className="lsa-section lsa-profile" aria-labelledby="lsa-profile-title">
          <SectionIntro
            eyebrow="Example profile"
            title="示例：不要只看平均分"
            description="以下为虚构示例。先关注自评与校准差异，再回到证据解释优势、盲点与机会。"
          />
          <div className="lsa-profile-legend" aria-label="图例">
            <span><i className="lsa-dot lsa-dot-self" />自评</span>
            <span><i className="lsa-dot lsa-dot-other" />外部校准</span>
          </div>
          <div className="lsa-profile-list">
            {article.profile.map((item) => (
              <article key={item.competency}>
                <header><h3>{item.competency}</h3><span>{item.status}</span></header>
                <div className="lsa-score-row">
                  <small>自评 {item.self.toFixed(1)}</small>
                  <div className="lsa-score-track"><i className="lsa-score-self" style={{ width: `${item.self * 20}%` }} /></div>
                </div>
                <div className="lsa-score-row">
                  <small>校准 {item.calibrated.toFixed(1)}</small>
                  <div className="lsa-score-track"><i className="lsa-score-other" style={{ width: `${item.calibrated * 20}%` }} /></div>
                </div>
              </article>
            ))}
          </div>
        </section>

        <section id="lsa-action" className="lsa-section lsa-action" aria-labelledby="lsa-action-title">
          <SectionIntro
            eyebrow="From insight to action"
            title="把一个发展重点转成可执行计划"
            description="借助 GROW 式问题收窄目标、看清现实、比较选项，并承诺下一步。"
          />
          <ol className="lsa-action-list">
            {article.actionPlan.map((item, index) => (
              <li key={item.phase}>
                <span>{String(index + 1).padStart(2, '0')}</span>
                <div>
                  <small>{item.phase}</small>
                  <h3>{item.question}</h3>
                  <p>{item.example}</p>
                </div>
              </li>
            ))}
          </ol>
          <aside className="lsa-action-check">
            <Target aria-hidden="true" />
            <div>
              <h3>行动完成标准</h3>
              <p>一个重点行为、一个真实练习场景、一位反馈伙伴，以及明确的 30 / 60 / 90 天检查点。</p>
            </div>
          </aside>
        </section>

        <section id="lsa-rollout" className="lsa-section lsa-rollout" aria-labelledby="lsa-rollout-title">
          <SectionIntro
            eyebrow="Organization rollout"
            title="从个人工具扩展为组织发展信号"
            description="聚合数据只能用于识别共同主题，并需要明确用途、匿名边界和后续发展投入。"
          />
          <div className="lsa-rollout-grid">
            {article.rollout.map((item, index) => (
              <article key={item.title}>
                <span>{String(index + 1).padStart(2, '0')}</span>
                <h3>{item.title}</h3>
                <p>{item.detail}</p>
              </article>
            ))}
          </div>
          <div className="lsa-boundary">
            <AlertTriangle aria-hidden="true" />
            <p><strong>不要这样使用：</strong>不要把自评结果直接作为绩效、晋升或人才标签，也不要公布可识别个人的群体结果。</p>
          </div>
        </section>

        <footer className="lsa-source-note">
          <div><ClipboardCheck aria-hidden="true" /><strong>方法来源</strong></div>
          {article.sources.map((source) => (
            <p key={source.url}>
              本文依据
              {' '}<a href={source.url} target="_blank" rel="noreferrer">{source.name}<ArrowUpRight aria-hidden="true" /></a>
              {' '}进行中文场景化重组。{source.note}访问日期：{source.accessedAt}。
            </p>
          ))}
        </footer>
      </div>
    </article>
  );
}

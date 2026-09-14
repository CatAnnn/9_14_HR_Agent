import {
  ClipboardCheck,
  FileText,
  ListChecks,
  Printer,
  Scale,
  ShieldCheck,
} from "lucide-react";
import { softSkillsAssessmentArticle as article } from "../../content/toolkit-articles/soft-skills-assessment";
import "../../styles/toolkit-soft-skills-assessment.css";

export function SoftSkillsAssessmentArticle() {
  return (
    <article className="ssa-article" aria-labelledby="ssa-overview-title">
      <section className="ssa-header" id="ssa-overview" aria-labelledby="ssa-overview-title">
        <p className="ssa-kicker">Toolkit Article</p>
        <h2 id="ssa-overview-title">{article.title}</h2>
        <p className="ssa-deck">{article.deck}</p>
        <div className="ssa-source" aria-label="资料来源">
          <span>来源：</span>
          <a href={article.source.url} target="_blank" rel="noreferrer">
            {article.source.label}
          </a>
          <span>访问日期：{article.source.accessedAt}</span>
        </div>
        <button className="ssa-print" type="button" onClick={() => window.print()}>
          <Printer size={16} aria-hidden="true" />
          打印
        </button>
      </section>

      <section className="ssa-section" id="ssa-model" aria-labelledby="ssa-model-title">
        <h2 id="ssa-model-title">
          <ListChecks size={20} aria-hidden="true" />
          评估模型
        </h2>
        <div className="ssa-model-grid">
          {article.model.map((item, index) => (
            <section className="ssa-model-step" key={item.title}>
              <span className="ssa-step-index">{index + 1}</span>
              <h3>{item.title}</h3>
              <p>{item.body}</p>
            </section>
          ))}
        </div>
      </section>

      <section className="ssa-section" id="ssa-profiles" aria-labelledby="ssa-profiles-title">
        <h2 id="ssa-profiles-title">
          <ClipboardCheck size={20} aria-hidden="true" />
          四项能力画像
        </h2>
        <div className="ssa-profile-list">
          {article.profiles.map((profile) => (
            <section className="ssa-profile" key={profile.name}>
              <div>
                <p className="ssa-profile-name">{profile.name}</p>
                <h3>{profile.cn}</h3>
              </div>
              <p>{profile.definition}</p>
              <ul>
                {profile.behaviors.map((behavior) => (
                  <li key={behavior}>{behavior}</li>
                ))}
              </ul>
            </section>
          ))}
        </div>
      </section>

      <section className="ssa-section" id="ssa-evidence" aria-labelledby="ssa-evidence-title">
        <h2 id="ssa-evidence-title">
          <FileText size={20} aria-hidden="true" />
          证据来源
        </h2>
        <ul className="ssa-evidence-list">
          {article.evidenceSources.map((source) => (
            <li key={source}>{source}</li>
          ))}
        </ul>
      </section>

      <section className="ssa-section" id="ssa-methods" aria-labelledby="ssa-methods-title">
        <h2 id="ssa-methods-title">方法选择</h2>
        <div className="ssa-details-list">
          {article.methods.map((method) => (
            <details className="ssa-details" key={method.title}>
              <summary>{method.title}</summary>
              <p>{method.body}</p>
            </details>
          ))}
        </div>
      </section>

      <section className="ssa-section" id="ssa-scenario" aria-labelledby="ssa-scenario-title">
        <h2 id="ssa-scenario-title">现实情境</h2>
        <div className="ssa-scenario">
          <h3>{article.scenario.title}</h3>
          <p>{article.scenario.setup}</p>
          <dl>
            <div>
              <dt>证据较强</dt>
              <dd>{article.scenario.strong}</dd>
            </div>
            <div>
              <dt>证据不足</dt>
              <dd>{article.scenario.weak}</dd>
            </div>
          </dl>
        </div>
      </section>

      <section className="ssa-section" id="ssa-scoring" aria-labelledby="ssa-scoring-title">
        <h2 id="ssa-scoring-title">
          <Scale size={20} aria-hidden="true" />
          评分与校准
        </h2>
        <p className="ssa-note">
          下表是内部五级行为量规，依据 OPM 的胜任力定义思路改写；它不是 OPM 官方量表。
        </p>
        <div className="ssa-table-wrap">
          <table className="ssa-rubric">
            <caption>五级行为量规</caption>
            <thead>
              <tr>
                <th scope="col">等级</th>
                <th scope="col">锚点</th>
                <th scope="col">行为证据</th>
              </tr>
            </thead>
            <tbody>
              {article.rubric.map(([level, anchor, evidence]) => (
                <tr key={level}>
                  <td data-label="等级">{level}</td>
                  <td data-label="锚点">{anchor}</td>
                  <td data-label="行为证据">{evidence}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <ul className="ssa-check-list">
          {article.scoring.map((item) => (
            <li key={item}>{item}</li>
          ))}
        </ul>
      </section>

      <section className="ssa-section" id="ssa-development" aria-labelledby="ssa-development-title">
        <h2 id="ssa-development-title">发展实验</h2>
        <div className="ssa-experiments">
          {article.development.map((item) => (
            <section key={item.title}>
              <h3>{item.title}</h3>
              <p>{item.body}</p>
            </section>
          ))}
        </div>
      </section>

      <section className="ssa-section" id="ssa-fairness" aria-labelledby="ssa-fairness-title">
        <h2 id="ssa-fairness-title">
          <ShieldCheck size={20} aria-hidden="true" />
          公平与隐私
        </h2>
        <ul className="ssa-check-list">
          {article.fairness.map((item) => (
            <li key={item}>{item}</li>
          ))}
        </ul>
      </section>
    </article>
  );
}

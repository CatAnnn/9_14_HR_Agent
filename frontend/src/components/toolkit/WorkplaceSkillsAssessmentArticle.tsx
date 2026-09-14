import {
  ArrowRight,
  BarChart3,
  BriefcaseBusiness,
  CheckCircle2,
  ChevronDown,
  ClipboardCheck,
  FileText,
  Gauge,
  Layers3,
  ListChecks,
  Route,
  Scale,
  ShieldCheck,
} from "lucide-react";
import { workplaceSkillsAssessmentArticle as article } from "../../content/toolkit-articles/workplace-skills-assessment";
import "../../styles/toolkit-workplace-skills-assessment.css";

export function WorkplaceSkillsAssessmentArticle() {
  return (
    <article className="wsa-article" aria-labelledby="wsa-overview-title">
      <section className="wsa-hero" id="wsa-overview" aria-labelledby="wsa-overview-title">
        <div className="wsa-hero__inner">
          <p className="wsa-kicker">{article.kicker}</p>
          <h2 id="wsa-overview-title">{article.title}</h2>
          <p className="wsa-deck">{article.deck}</p>
          <p className="wsa-source-note">{article.sourceNote}</p>
        </div>
      </section>

      <section className="wsa-section wsa-section--lead" aria-labelledby="wsa-thesis">
        <div className="wsa-section__header">
          <span className="wsa-section__icon" aria-hidden="true">
            <Gauge size={20} />
          </span>
          <div>
            <p className="wsa-section__eyebrow">Management premise</p>
            <h2 id="wsa-thesis">技能评估是一套经营控制系统</h2>
          </div>
        </div>
        <p>
          好的技能评估不是把员工放进一个统一测试里排名，而是把组织真正需要完成的工作拆清楚，再用足够接近工作的证据判断当前能力、未来准备度和发展投资顺序。它既服务员工成长，也服务岗位设计、招聘标准、项目调配和组织能力建设。
        </p>
        <ul className="wsa-principles" aria-label="设计原则">
          {article.principles.map((principle) => (
            <li key={principle}>
              <CheckCircle2 size={18} aria-hidden="true" />
              <span>{principle}</span>
            </li>
          ))}
        </ul>
      </section>

      <section className="wsa-section" id="wsa-operating-model" aria-labelledby="wsa-operating-title">
        <div className="wsa-section__header">
          <span className="wsa-section__icon" aria-hidden="true">
            <ArrowRight size={20} />
          </span>
          <div>
            <p className="wsa-section__eyebrow">Job → Tasks → Skills → Evidence → Gap → Development</p>
            <h2 id="wsa-operating-title">六段式主线</h2>
          </div>
        </div>
        <ol className="wsa-process">
          {article.processSteps.map((step, index) => (
            <li key={step.id} className="wsa-process__item">
              <div className="wsa-process__index" aria-label={`步骤 ${index + 1}`}>
                {index + 1}
              </div>
              <div className="wsa-process__body">
                <h3>{step.title}</h3>
                <p>{step.premise}</p>
                <p className="wsa-process__output">
                  <strong>输出：</strong>
                  {step.output}
                </p>
                <ul>
                  {step.checks.map((check) => (
                    <li key={check}>{check}</li>
                  ))}
                </ul>
              </div>
            </li>
          ))}
        </ol>
      </section>

      <section className="wsa-section" id="wsa-role-canvas" aria-labelledby="wsa-role-title">
        <div className="wsa-section__header">
          <span className="wsa-section__icon" aria-hidden="true">
            <BriefcaseBusiness size={20} />
          </span>
          <div>
            <p className="wsa-section__eyebrow">Role canvas</p>
            <h2 id="wsa-role-title">一页岗位画布</h2>
          </div>
        </div>
        <div className="wsa-canvas">
          {article.roleCanvas.map((item) => (
            <section className="wsa-canvas__cell" key={item.label} aria-labelledby={`wsa-canvas-${item.label}`}>
              <h3 id={`wsa-canvas-${item.label}`}>{item.label}</h3>
              <p>{item.prompt}</p>
              <p className="wsa-muted">{item.example}</p>
            </section>
          ))}
        </div>
      </section>

      <section className="wsa-section" id="wsa-architecture" aria-labelledby="wsa-architecture-title">
        <div className="wsa-section__header">
          <span className="wsa-section__icon" aria-hidden="true">
            <Layers3 size={20} />
          </span>
          <div>
            <p className="wsa-section__eyebrow">Technical and core skills</p>
            <h2 id="wsa-architecture-title">技术技能与核心技能架构</h2>
          </div>
        </div>
        <div className="wsa-architecture">
          <section className="wsa-architecture__column" aria-labelledby="wsa-tech-title">
            <h3 id="wsa-tech-title">技术技能</h3>
            <ul>
              {article.architecture.technical.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          </section>
          <section className="wsa-architecture__column" aria-labelledby="wsa-core-title">
            <h3 id="wsa-core-title">核心技能</h3>
            <ul>
              {article.architecture.core.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          </section>
        </div>
        <p className="wsa-callout">{article.architecture.note}</p>
      </section>

      <section className="wsa-section" id="wsa-rubrics" aria-labelledby="wsa-rubrics-title">
        <div className="wsa-section__header">
          <span className="wsa-section__icon" aria-hidden="true">
            <Scale size={20} />
          </span>
          <div>
            <p className="wsa-section__eyebrow">Rubrics</p>
            <h2 id="wsa-rubrics-title">内部五级标尺与 SFIA 七级参考</h2>
          </div>
        </div>
        <div className="wsa-table-wrap">
          <table className="wsa-table">
            <caption>内部五级能力标尺</caption>
            <thead>
              <tr>
                <th scope="col">等级</th>
                <th scope="col">名称</th>
                <th scope="col">表现信号</th>
                <th scope="col">建议证据</th>
              </tr>
            </thead>
            <tbody>
              {article.internalRubric.map((level) => (
                <tr key={level.level}>
                  <td data-label="等级">{level.level}</td>
                  <td data-label="名称">{level.name}</td>
                  <td data-label="表现信号">{level.signal}</td>
                  <td data-label="建议证据">{level.evidence}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <aside className="wsa-reference" aria-label="SFIA 七级责任参考">
          <h3>SFIA 七级责任参考</h3>
          <p>{article.sfiaReference.note}</p>
          <ol>
            {article.sfiaReference.levels.map((level) => (
              <li key={level}>{level}</li>
            ))}
          </ol>
        </aside>
      </section>

      <section className="wsa-section" id="wsa-methods" aria-labelledby="wsa-methods-title">
        <div className="wsa-section__header">
          <span className="wsa-section__icon" aria-hidden="true">
            <ClipboardCheck size={20} />
          </span>
          <div>
            <p className="wsa-section__eyebrow">Assessment methods</p>
            <h2 id="wsa-methods-title">方法矩阵</h2>
          </div>
        </div>
        <div className="wsa-table-wrap">
          <table className="wsa-table">
            <caption>不同评估方法的适用边界</caption>
            <thead>
              <tr>
                <th scope="col">方法</th>
                <th scope="col">最适合</th>
                <th scope="col">核心证据</th>
                <th scope="col">控制点</th>
              </tr>
            </thead>
            <tbody>
              {article.methodMatrix.map((row) => (
                <tr key={row.method}>
                  <td data-label="方法">{row.method}</td>
                  <td data-label="最适合">{row.bestFor}</td>
                  <td data-label="核心证据">{row.evidence}</td>
                  <td data-label="控制点">{row.controls}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section className="wsa-section" id="wsa-work-sample" aria-labelledby="wsa-work-sample-title">
        <div className="wsa-section__header">
          <span className="wsa-section__icon" aria-hidden="true">
            <FileText size={20} />
          </span>
          <div>
            <p className="wsa-section__eyebrow">Real-work sample</p>
            <h2 id="wsa-work-sample-title">{article.workSample.title}</h2>
          </div>
        </div>
        <div className="wsa-sample">
          <div>
            <h3>场景</h3>
            <p>{article.workSample.scenario}</p>
            <h3>任务</h3>
            <p>{article.workSample.task}</p>
          </div>
          <div>
            <h3>材料包</h3>
            <ul>
              {article.workSample.materials.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          </div>
        </div>
        <ol className="wsa-scoring" aria-label="评分维度">
          {article.workSample.scoring.map((item) => (
            <li key={item}>{item}</li>
          ))}
        </ol>
      </section>

      <section className="wsa-section" id="wsa-portfolio" aria-labelledby="wsa-portfolio-title">
        <div className="wsa-section__header">
          <span className="wsa-section__icon" aria-hidden="true">
            <ListChecks size={20} />
          </span>
          <div>
            <p className="wsa-section__eyebrow">Evidence portfolio</p>
            <h2 id="wsa-portfolio-title">证据组合包</h2>
          </div>
        </div>
        <div className="wsa-portfolio">
          {article.portfolio.map((item) => (
            <section className="wsa-portfolio__item" key={item.layer} aria-labelledby={`wsa-portfolio-${item.layer}`}>
              <h3 id={`wsa-portfolio-${item.layer}`}>{item.layer}</h3>
              <p>{item.examples}</p>
              <p className="wsa-muted">{item.value}</p>
            </section>
          ))}
        </div>
      </section>

      <section className="wsa-section" id="wsa-gap" aria-labelledby="wsa-gap-title">
        <div className="wsa-section__header">
          <span className="wsa-section__icon" aria-hidden="true">
            <BarChart3 size={20} />
          </span>
          <div>
            <p className="wsa-section__eyebrow">Gap priority</p>
            <h2 id="wsa-gap-title">差距优先级</h2>
          </div>
        </div>
        <div className="wsa-gap-grid">
          {article.gapPriority.map((item) => (
            <section className="wsa-gap" key={item.name} aria-labelledby={`wsa-gap-${item.name}`}>
              <h3 id={`wsa-gap-${item.name}`}>{item.name}</h3>
              <p>{item.rule}</p>
              <p className="wsa-gap__action">{item.action}</p>
            </section>
          ))}
        </div>
      </section>

      <section className="wsa-section" id="wsa-development" aria-labelledby="wsa-development-title">
        <div className="wsa-section__header">
          <span className="wsa-section__icon" aria-hidden="true">
            <Route size={20} />
          </span>
          <div>
            <p className="wsa-section__eyebrow">Development and governance</p>
            <h2 id="wsa-development-title">发展路线与治理</h2>
          </div>
        </div>
        <div className="wsa-routes">
          {article.developmentRoutes.map((item) => (
            <section className="wsa-route" key={item.route} aria-labelledby={`wsa-route-${item.route}`}>
              <h3 id={`wsa-route-${item.route}`}>{item.route}</h3>
              <p>{item.use}</p>
              <p className="wsa-muted">{item.pattern}</p>
            </section>
          ))}
        </div>
        <div className="wsa-governance" aria-label="治理要求">
          {article.governance.map((item) => (
            <details key={item.title} className="wsa-details">
              <summary>
                <span>
                  <ShieldCheck size={18} aria-hidden="true" />
                  {item.title}
                </span>
                <ChevronDown size={18} aria-hidden="true" />
              </summary>
              <p>{item.body}</p>
            </details>
          ))}
        </div>
      </section>

      <footer className="wsa-sources" id="wsa-sources" aria-labelledby="wsa-sources-title">
        <h2 id="wsa-sources-title">来源记录</h2>
        <ul>
          {article.sources.map((source) => (
            <li key={source.url}>
              <a href={source.url} target="_blank" rel="noreferrer">
                {source.title}
              </a>
              <span>访问日期：{source.accessed}</span>
            </li>
          ))}
        </ul>
      </footer>
    </article>
  );
}

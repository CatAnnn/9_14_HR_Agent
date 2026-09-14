import {
  AlertTriangle,
  ArrowRight,
  Check,
  ExternalLink,
  Eye,
  Layers3,
  Link2,
  RefreshCw,
  Scale,
  ShieldCheck,
  UsersRound,
  Workflow,
} from 'lucide-react';
import { useState, type KeyboardEvent } from 'react';
import {
  belbinCollaborationPairs,
  belbinExampleTeam,
  belbinProjectStages,
  belbinRoleGroups,
  belbinSafeguards,
  belbinSource,
  belbinWorkshopSteps,
  type BelbinRoleGroupId,
} from '../../content/toolkit-articles/belbin-team-roles';
import '../../styles/toolkit-belbin-team-roles.css';

export function BelbinTeamRolesArticle() {
  const [activeGroupId, setActiveGroupId] = useState<BelbinRoleGroupId>('social');
  const [discussedRoleIds, setDiscussedRoleIds] = useState<string[]>([]);
  const activeGroup = belbinRoleGroups.find((group) => group.id === activeGroupId) ?? belbinRoleGroups[0];

  const toggleCoverage = (roleId: string) => {
    setDiscussedRoleIds((current) => (
      current.includes(roleId)
        ? current.filter((id) => id !== roleId)
        : [...current, roleId]
    ));
  };

  const handleGroupKeyDown = (
    event: KeyboardEvent<HTMLButtonElement>,
    currentIndex: number,
  ) => {
    let nextIndex: number;

    switch (event.key) {
      case 'ArrowRight':
        nextIndex = (currentIndex + 1) % belbinRoleGroups.length;
        break;
      case 'ArrowLeft':
        nextIndex = (currentIndex - 1 + belbinRoleGroups.length) % belbinRoleGroups.length;
        break;
      case 'Home':
        nextIndex = 0;
        break;
      case 'End':
        nextIndex = belbinRoleGroups.length - 1;
        break;
      default:
        return;
    }

    event.preventDefault();
    setActiveGroupId(belbinRoleGroups[nextIndex].id);
    event.currentTarget.parentElement
      ?.querySelectorAll<HTMLButtonElement>('[role="tab"]')[nextIndex]
      ?.focus();
  };

  return (
    <article className="belbin-article" aria-label="Belbin 团队角色知识说明与覆盖规划">
      <section id="belbin-foundation" className="belbin-section belbin-foundation" aria-labelledby="belbin-foundation-title">
        <div className="belbin-inner">
          <header className="belbin-section-heading">
            <span className="belbin-eyebrow"><Eye aria-hidden="true" /> Framework boundary</span>
            <h2 id="belbin-foundation-title">先看团队需要什么贡献，再讨论谁能在何时提供</h2>
            <p>
              Belbin 团队角色把有助于团队推进的行为归纳为九类贡献。它提供一套讨论协作的语言，
              但不等同于人格标签、岗位能力或绩效结论。
            </p>
          </header>

          <div className="belbin-foundation-grid">
            <div className="belbin-principles" aria-label="使用原则">
              <div>
                <strong>行为是情境性的</strong>
                <p>同一个人在不同目标、团队和压力下，可能展现不同的贡献模式。</p>
              </div>
              <div>
                <strong>优势伴随取舍</strong>
                <p>每类有价值的贡献都可能带来相应风险，重点是让优势被使用、让风险被管理。</p>
              </div>
              <div>
                <strong>团队不等于九个人</strong>
                <p>一个成员可以提供多种贡献，也不是所有阶段都同时需要九类行为。</p>
              </div>
            </div>

            <aside className="belbin-boundary-note" aria-label="版权与评估边界">
              <ShieldCheck aria-hidden="true" />
              <div>
                <h3>这是知识说明，不是 Belbin 测试</h3>
                <p>{belbinSource.copyright}</p>
              </div>
            </aside>
          </div>
        </div>
      </section>

      <section id="belbin-role-explorer" className="belbin-section belbin-role-explorer" aria-labelledby="belbin-role-explorer-title">
        <div className="belbin-inner">
          <header className="belbin-section-heading belbin-section-heading-split">
            <div>
              <span className="belbin-eyebrow"><Layers3 aria-hidden="true" /> Nine contributions</span>
              <h2 id="belbin-role-explorer-title">九类团队贡献，分为关系、思考与行动三组</h2>
            </div>
            <p>切换分组查看贡献、优势与需要共同管理的风险。描述均为场景化概括，不用于判断个人类型。</p>
          </header>

          <div className="belbin-tabs" role="tablist" aria-label="团队角色分组">
            {belbinRoleGroups.map((group, index) => {
              const isActive = group.id === activeGroup.id;
              return (
                <button
                  key={group.id}
                  id={`belbin-tab-${group.id}`}
                  type="button"
                  role="tab"
                  aria-selected={isActive}
                  aria-controls={`belbin-panel-${group.id}`}
                  tabIndex={isActive ? 0 : -1}
                  onClick={() => setActiveGroupId(group.id)}
                  onKeyDown={(event) => handleGroupKeyDown(event, index)}
                >
                  <span>{group.label}</span>
                  <small>{group.englishLabel}</small>
                </button>
              );
            })}
          </div>

          <div
            id={`belbin-panel-${activeGroup.id}`}
            className="belbin-role-panel"
            role="tabpanel"
            aria-labelledby={`belbin-tab-${activeGroup.id}`}
          >
            <p className="belbin-group-summary">{activeGroup.summary}</p>
            <div className="belbin-role-grid">
              {activeGroup.roles.map((role) => (
                <article className="belbin-role-card" key={role.id}>
                  <header>
                    <span>{role.englishName}</span>
                    <h3>{role.name}</h3>
                  </header>
                  <p className="belbin-role-contribution">{role.contribution}</p>
                  <dl>
                    <div>
                      <dt><Check aria-hidden="true" /> 有效优势</dt>
                      <dd>{role.strength}</dd>
                    </div>
                    <div>
                      <dt><Scale aria-hidden="true" /> 相伴风险</dt>
                      <dd>{role.allowableRisk}</dd>
                    </div>
                  </dl>
                  <p className="belbin-role-question"><strong>团队可讨论：</strong>{role.teamQuestion}</p>
                </article>
              ))}
            </div>
          </div>
        </div>
      </section>

      <section id="belbin-coverage" className="belbin-section belbin-coverage" aria-labelledby="belbin-coverage-title">
        <div className="belbin-inner belbin-coverage-layout">
          <header className="belbin-section-heading">
            <span className="belbin-eyebrow"><Workflow aria-hidden="true" /> Coverage planning</span>
            <h2 id="belbin-coverage-title">把它当作贡献清单，而不是个人评分表</h2>
            <p>
              根据当前目标，标记团队已经讨论过的贡献。这里只记录“是否纳入工作设计”，
              不计算角色分数，也不把贡献绑定为任何人的固定身份。
            </p>
            <button
              className="belbin-reset-button"
              type="button"
              onClick={() => setDiscussedRoleIds([])}
              disabled={discussedRoleIds.length === 0}
            >
              <RefreshCw aria-hidden="true" /> 清除讨论标记
            </button>
          </header>

          <div className="belbin-coverage-board" aria-label="非诊断性团队贡献覆盖清单">
            {belbinRoleGroups.map((group) => (
              <section key={group.id} aria-labelledby={`belbin-coverage-${group.id}`}>
                <h3 id={`belbin-coverage-${group.id}`}>{group.label}</h3>
                <div>
                  {group.roles.map((role) => {
                    const isDiscussed = discussedRoleIds.includes(role.id);
                    return (
                      <button
                        key={role.id}
                        type="button"
                        aria-pressed={isDiscussed}
                        onClick={() => toggleCoverage(role.id)}
                      >
                        <span>{role.name}</span>
                        <small>{isDiscussed ? '已纳入讨论' : '待讨论'}</small>
                      </button>
                    );
                  })}
                </div>
              </section>
            ))}
            <p className="belbin-coverage-status" aria-live="polite">
              {discussedRoleIds.length === 0
                ? '尚未标记。请先从项目目标和阶段需要出发。'
                : '标记仅代表团队已讨论该类贡献，不代表任何人的角色测评结果。'}
            </p>
          </div>
        </div>
      </section>

      <section id="belbin-project" className="belbin-section belbin-project" aria-labelledby="belbin-project-title">
        <div className="belbin-inner">
          <header className="belbin-section-heading">
            <span className="belbin-eyebrow"><Workflow aria-hidden="true" /> Project stages</span>
            <h2 id="belbin-project-title">不同阶段，需要被放大的贡献不同</h2>
            <p>下面是工作设计示例，不是官方角色分配规则。团队应根据目标、风险和现有能力调整。</p>
          </header>

          <div className="belbin-table-wrap">
            <table className="belbin-stage-table">
              <caption className="belbin-sr-only">项目阶段与团队贡献需求示例</caption>
              <thead>
                <tr>
                  <th scope="col">项目阶段</th>
                  <th scope="col">核心需要</th>
                  <th scope="col">可关注的贡献</th>
                  <th scope="col">团队检查点</th>
                </tr>
              </thead>
              <tbody>
                {belbinProjectStages.map((item) => (
                  <tr key={item.stage}>
                    <th scope="row" data-label="项目阶段">{item.stage}</th>
                    <td data-label="核心需要">{item.need}</td>
                    <td data-label="可关注的贡献">{item.contributions}</td>
                    <td data-label="团队检查点">{item.check}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </section>

      <section id="belbin-collaboration" className="belbin-section belbin-collaboration" aria-labelledby="belbin-collaboration-title">
        <div className="belbin-inner">
          <header className="belbin-section-heading belbin-section-heading-split">
            <div>
              <span className="belbin-eyebrow"><Link2 aria-hidden="true" /> Complementary work</span>
              <h2 id="belbin-collaboration-title">互补不是相互抵消，而是建立工作约定</h2>
            </div>
            <p>角色语言最有价值的时刻，是团队能把差异转化为可执行的协作方式。</p>
          </header>

          <div className="belbin-pair-list">
            {belbinCollaborationPairs.map((pair, index) => (
              <article key={pair.title}>
                <span>{String(index + 1).padStart(2, '0')}</span>
                <div>
                  <h3>{pair.title}</h3>
                  <p>{pair.value}</p>
                  <small><strong>建议约定：</strong>{pair.agreement}</small>
                </div>
              </article>
            ))}
          </div>

          <aside className="belbin-example" aria-labelledby="belbin-example-title">
            <div>
              <span className="belbin-eyebrow"><UsersRound aria-hidden="true" /> Worked example</span>
              <h3 id="belbin-example-title">{belbinExampleTeam.title}</h3>
              <p>{belbinExampleTeam.context}</p>
            </div>
            <div className="belbin-example-columns">
              <section aria-labelledby="belbin-observations-title">
                <h4 id="belbin-observations-title">可观察现象</h4>
                <ul>{belbinExampleTeam.observations.map((item) => <li key={item}>{item}</li>)}</ul>
              </section>
              <section aria-labelledby="belbin-actions-title">
                <h4 id="belbin-actions-title">对应工作设计</h4>
                <ul>{belbinExampleTeam.actions.map((item) => <li key={item}>{item}</li>)}</ul>
              </section>
            </div>
          </aside>
        </div>
      </section>

      <section id="belbin-workshop" className="belbin-section belbin-workshop" aria-labelledby="belbin-workshop-title">
        <div className="belbin-inner belbin-workshop-layout">
          <header className="belbin-section-heading">
            <span className="belbin-eyebrow"><UsersRound aria-hidden="true" /> Team workshop</span>
            <h2 id="belbin-workshop-title">用 60 分钟把角色语言转化为团队约定</h2>
            <p>主持人始终把讨论拉回“当前目标需要什么行为”，不要求成员自我归类。</p>
          </header>
          <ol className="belbin-workshop-steps">
            {belbinWorkshopSteps.map((step, index) => (
              <li key={step.title}>
                <span>{String(index + 1).padStart(2, '0')}</span>
                <div><h3>{step.title}</h3><p>{step.detail}</p></div>
              </li>
            ))}
          </ol>
        </div>
      </section>

      <section id="belbin-guardrails" className="belbin-section belbin-guardrails" aria-labelledby="belbin-guardrails-title">
        <div className="belbin-inner">
          <header className="belbin-section-heading">
            <span className="belbin-eyebrow"><AlertTriangle aria-hidden="true" /> Responsible use</span>
            <h2 id="belbin-guardrails-title">五条边界，避免把协作语言变成标签</h2>
          </header>
          <div className="belbin-safeguard-list">
            {belbinSafeguards.map((item, index) => (
              <article key={item.title}>
                <span>{String(index + 1).padStart(2, '0')}</span>
                <div><h3>{item.title}</h3><p>{item.detail}</p></div>
              </article>
            ))}
          </div>

          <footer className="belbin-source-note">
            <div>
              <strong>参考来源</strong>
              <p>{belbinSource.note}</p>
              <small>访问日期：{belbinSource.accessedAt}</small>
            </div>
            <a href={belbinSource.url} target="_blank" rel="noopener noreferrer">
              查看 {belbinSource.organization} 官方说明
              <ExternalLink aria-hidden="true" />
            </a>
          </footer>
        </div>
      </section>

      <a className="belbin-back-to-top" href="#belbin-foundation">
        回到本文开头 <ArrowRight aria-hidden="true" />
      </a>
    </article>
  );
}

export default BelbinTeamRolesArticle;
